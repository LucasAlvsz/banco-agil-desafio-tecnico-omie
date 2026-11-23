import os
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from math import ceil
from pathlib import Path
from shutil import copyfile
from statistics import mean
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal
from unittest.mock import patch
from uuid import uuid4

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langsmith import tracing_context
from pydantic import Field, TypeAdapter

from banco_agil.agents.graph import ERROR_REPLY, TurnResult
from banco_agil.config import Settings
from banco_agil.evals.models import (
    EvalLimitRequest,
    EvalReport,
    EvalScenario,
    ScenarioEvalResult,
)
from banco_agil.infrastructure.csv_repositories import (
    CsvClientRepository,
    CsvLimitRequestRepository,
)
from banco_agil.infrastructure.exchange_client import (
    AWESOME_URL,
    FRANKFURTER_URL,
    ExchangeRateClient,
)
from banco_agil.infrastructure.observability import TurnObservability, create_langfuse_observability
from banco_agil.infrastructure.privacy import contains_pii
from banco_agil.mcp import McpExchangeRateProvider
from banco_agil.runtime import ConversationRuntime

DEFAULT_SCENARIOS_PATH = Path("evals/scenarios.json")
SEEDS = ("clientes.csv", "score_limite.csv", "solicitacoes_aumento_limite.csv")


class EvalScriptedChatModel(BaseChatModel):
    responses: deque[AIMessage] = Field(default_factory=deque)

    @property
    def _llm_type(self) -> str:
        return "eval-scripted"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.responses.popleft())])


def load_scenarios(path: Path = DEFAULT_SCENARIOS_PATH) -> list[EvalScenario]:
    scenarios = TypeAdapter(Annotated[list[EvalScenario], Field(min_length=1)]).validate_json(
        path.read_bytes()
    )
    if len({scenario.id for scenario in scenarios}) != len(scenarios):
        raise ValueError("Os IDs dos cenários devem ser únicos")
    return scenarios


def _exchange_provider(scenario: EvalScenario, timeout: float) -> McpExchangeRateProvider:
    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        if url == f"{AWESOME_URL}/USD-BRL":
            if scenario.exchange == "fallback":
                raise httpx.ReadTimeout("Timeout simulado", request=request)
            return httpx.Response(
                200,
                json={
                    "USDBRL": {
                        "name": "Dólar Americano/Real Brasileiro",
                        "bid": "5.25",
                        "ask": "5.30",
                        "create_date": "2026-11-21 10:30:00",
                    }
                },
            )
        if (
            url == FRANKFURTER_URL
            and scenario.exchange == "fallback"
            and request.url.params == httpx.QueryParams(base="USD", symbols="BRL")
        ):
            return httpx.Response(200, json={"date": "2026-11-20", "rates": {"BRL": 5.20}})
        return httpx.Response(404)

    return McpExchangeRateProvider.in_memory(
        ExchangeRateClient(timeout, httpx.MockTransport(respond))
    )


def _requests(requests: CsvLimitRequestRepository) -> tuple[EvalLimitRequest, ...]:
    return tuple(
        EvalLimitRequest.model_validate(request.model_dump(exclude={"data_hora_solicitacao"}))
        for request in requests.list_all()
    )


def _aggregate_cost(costs: Sequence[float | None]) -> float | None:
    return None if None in costs else sum(cost for cost in costs if cost is not None)


def _run_scenario(
    scenario: EvalScenario,
    settings: Settings,
    *,
    live_model: BaseChatModel | None,
    observability: TurnObservability,
) -> ScenarioEvalResult:
    work_dir = settings.data_dir
    clients_path, _, requests_path = (work_dir / name for name in SEEDS)
    clients = CsvClientRepository(clients_path)
    requests = CsvLimitRequestRepository(requests_path)
    seeds = clients.list_all()
    cpfs = {client.cpf for client in seeds}
    dates = {client.data_nascimento for client in seeds}
    scripted = EvalScriptedChatModel() if live_model is None else None
    model = scripted if scripted is not None else live_model
    assert model is not None
    thread_id = str(uuid4())
    failures: list[str] = []
    results: list[TurnResult] = []
    route_matches = pii_leaks = 0
    message_count = 0

    with ConversationRuntime.from_settings(
        settings,
        model=model,
        exchange_provider=_exchange_provider(scenario, settings.exchange_timeout_seconds),
        observability=observability,
    ) as runtime:
        for index, turn in enumerate(scenario.turns, start=1):
            expect = turn.expect
            before = {name: (work_dir / name).read_bytes() for name in expect.unchanged}
            if scripted is not None:
                for step in turn.script:
                    message = AIMessage(
                        content=step if isinstance(step, str) else "",
                        usage_metadata={
                            "input_tokens": 120,
                            "output_tokens": 20,
                            "total_tokens": 140,
                        },
                    )
                    if not isinstance(step, str):
                        message.tool_calls = [
                            {
                                "name": step.tool,
                                "args": step.args,
                                "id": str(uuid4()),
                                "type": "tool_call",
                            }
                        ]
                    scripted.responses.append(message)

            if turn.message is not None:
                result = runtime.run_turn(thread_id=thread_id, user_message=turn.message)
            else:
                assert turn.approve is not None
                result = runtime.resume_turn(thread_id=thread_id, approved=turn.approve)
            results.append(result)
            state = runtime.get_state(thread_id)
            messages = state.get("messages", [])
            tool_text = "\n".join(
                message.text
                for message in messages[message_count:]
                if isinstance(message, ToolMessage)
            )
            message_count = len(messages)
            pending = result.pending_confirmation
            telemetry = result.telemetry
            route_ok = telemetry.agent_route == expect.route
            route_matches += route_ok
            leaked = contains_pii(result.reply, cpfs=cpfs, birth_dates=dates)
            pii_leaks += leaked

            checks = {
                "resposta sem erro": result.reply != ERROR_REPLY,
                "autenticação": result.authenticated == expect.authenticated,
                "encerramento": result.conversation_ended == expect.ended,
                "agente ativo": result.active_agent == expect.active_agent,
                "rota": route_ok,
                "guardrail": telemetry.guardrail_blocked == expect.guardrail_blocked,
                "tokens": expect.total_tokens is None
                or telemetry.total_tokens == expect.total_tokens,
                "confirmação": (pending.novo_limite_solicitado if pending else None)
                == expect.confirmation_amount,
                "valor para reenvio": state.get("pending_requested_limit")
                == expect.pending_requested_limit,
                "pedidos no CSV": _requests(requests) == expect.requests,
                "CSVs preservados": all(
                    (work_dir / name).read_bytes() == data for name, data in before.items()
                ),
                "resposta das ferramentas": all(part in tool_text for part in expect.tool_contains),
                "resposta sem PII": not leaked,
            }
            if expect.auth_attempts is not None:
                checks["tentativas de autenticação"] = (
                    state.get("auth_attempts", 0) == expect.auth_attempts
                )
            if expect.tools is not None:
                checks["ferramentas chamadas"] = telemetry.tools_called == expect.tools
            if expect.client_limit is not None or expect.client_score is not None:
                cpf = state.get("client_cpf")
                client = clients.get(cpf) if cpf else None
                checks["limite do cliente"] = expect.client_limit is None or (
                    client is not None and client.limite_atual == expect.client_limit
                )
                checks["score do cliente"] = expect.client_score is None or (
                    client is not None and client.score == expect.client_score
                )
            if scripted is not None:
                checks["resposta do roteiro"] = all(
                    part in result.reply for part in expect.reply_contains
                )
                checks["roteiro consumido"] = not scripted.responses
                scripted.responses.clear()
            failures.extend(
                f"Turno {index} · {check}" for check, passed in checks.items() if not passed
            )

    return ScenarioEvalResult(
        id=scenario.id,
        passed=not failures,
        failures=tuple(failures),
        route_matches=route_matches,
        turns=len(results),
        pii_leaks=pii_leaks,
        latencies_ms=tuple(result.telemetry.latency_ms for result in results),
        total_tokens=sum(result.telemetry.total_tokens for result in results),
        total_cost_usd=_aggregate_cost([result.telemetry.estimated_cost_usd for result in results]),
    )


def run_evals(
    *,
    scenarios_path: Path = DEFAULT_SCENARIOS_PATH,
    mode: Literal["offline", "live"] = "offline",
    settings: Settings | None = None,
) -> EvalReport:
    if mode not in ("offline", "live"):
        raise ValueError("Modo de avaliação inválido")
    scenarios = load_scenarios(scenarios_path)
    offline = mode == "offline"
    model = None
    if offline:
        # Offline evals use known prices and do not load live configuration.
        offline_model = "openai/gpt-4o-mini"
        runtime_settings = (
            Settings.model_construct(llm_model=offline_model)
            if settings is None
            else settings.model_copy(update={"llm_model": offline_model})
        )
        observability = TurnObservability()
    else:
        from banco_agil.infrastructure.llm_factory import create_chat_model

        runtime_settings = settings if settings is not None else Settings()
        model = create_chat_model(runtime_settings)
        observability = TurnObservability(create_langfuse_observability(runtime_settings))
    offline_env = {
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "LITELLM_OFFLINE_MODE": "True",
        "LITELLM_TELEMETRY": "False",
    }
    results = []
    try:
        with (
            patch.dict(os.environ, offline_env) if offline else nullcontext(),
            tracing_context(enabled=False) if offline else nullcontext(),
        ):
            for scenario in scenarios:
                with TemporaryDirectory(prefix="banco-agil-eval-") as directory:
                    work_dir = Path(directory)
                    for name in SEEDS:
                        copyfile(runtime_settings.data_dir / name, work_dir / name)
                    results.append(
                        _run_scenario(
                            scenario,
                            runtime_settings.model_copy(update={"data_dir": work_dir}),
                            live_model=model,
                            observability=observability,
                        )
                    )
    finally:
        observability.flush()

    latencies = sorted(latency for result in results for latency in result.latencies_ms)
    passed = sum(result.passed for result in results)
    return EvalReport(
        scenarios=tuple(results),
        total_scenarios=len(results),
        passed_scenarios=passed,
        failed_scenarios=len(results) - passed,
        pass_rate=passed / len(results),
        route_accuracy=sum(result.route_matches for result in results) / len(latencies),
        pii_leaks=sum(result.pii_leaks for result in results),
        avg_latency_ms=mean(latencies),
        p95_latency_ms=latencies[ceil(len(latencies) * 0.95) - 1],
        total_tokens=sum(result.total_tokens for result in results),
        total_cost_usd=_aggregate_cost([result.total_cost_usd for result in results]),
    )

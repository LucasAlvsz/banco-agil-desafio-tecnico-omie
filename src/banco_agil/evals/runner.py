import os
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
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
    EvalExpectations,
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
    awesome_quotes = {
        f"{AWESOME_URL}/USD-BRL": {
            "USDBRL": {
                "name": "Dólar Americano/Real Brasileiro",
                "bid": "5.25",
                "ask": "5.30",
                "create_date": "2026-11-21 10:30:00",
            }
        },
        f"{AWESOME_URL}/EUR-BRL": {
            "EURBRL": {
                "name": "Euro/Real Brasileiro",
                "bid": "6.10",
                "ask": "6.15",
                "create_date": "2026-11-21 10:30:00",
            }
        },
    }
    frankfurter_rates = {"USD": 5.20, "EUR": 6.05}

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        if url in awesome_quotes:
            if scenario.exchange == "fallback":
                raise httpx.ReadTimeout("Timeout simulado", request=request)
            return httpx.Response(200, json=awesome_quotes[url])
        base = request.url.params.get("base")
        if (
            url == FRANKFURTER_URL
            and scenario.exchange == "fallback"
            and base in frankfurter_rates
            and request.url.params == httpx.QueryParams(base=base, symbols="BRL")
        ):
            return httpx.Response(
                200, json={"date": "2026-11-20", "rates": {"BRL": frankfurter_rates[base]}}
            )
        return httpx.Response(404)

    return McpExchangeRateProvider.in_memory(
        ExchangeRateClient(timeout, httpx.MockTransport(respond))
    )


def _list_eval_requests(repository: CsvLimitRequestRepository) -> tuple[EvalLimitRequest, ...]:
    return tuple(
        EvalLimitRequest.model_validate(request.model_dump(exclude={"data_hora_solicitacao"}))
        for request in repository.list_all()
    )


def _aggregate_cost(costs: Sequence[float | None]) -> float | None:
    return None if None in costs else sum(cost for cost in costs if cost is not None)


def _request_summary(requests: Sequence[EvalLimitRequest]) -> list[tuple[float, str]]:
    return [(item.novo_limite_solicitado, item.status_pedido.value) for item in requests]


def _collect_turn_failures(
    *,
    index: int,
    expect: EvalExpectations,
    result: TurnResult,
    state: Mapping[str, Any],
    route_ok: bool,
    leaked: bool,
    tool_text: str,
    changed_files: Sequence[str],
    clients: CsvClientRepository,
    requests: CsvLimitRequestRepository,
    scripted: EvalScriptedChatModel | None,
) -> list[str]:
    failures: list[str] = []

    def check(name: str, passed: bool, detail: str | None = None) -> None:
        if not passed:
            suffix = f" ({detail})" if detail is not None else ""
            failures.append(f"Turno {index} · {name}{suffix}")

    def check_eq(name: str, expected: object, actual: object) -> None:
        check(name, expected == actual, f"esperado={expected}, obtido={actual}")

    pending = result.pending_confirmation
    telemetry = result.telemetry
    confirmation_amount = pending.novo_limite_solicitado if pending else None
    actual_requests = _list_eval_requests(requests)
    missing_tool_parts = [part for part in expect.tool_contains if part not in tool_text]

    check("resposta sem erro", result.reply != ERROR_REPLY)
    check_eq("autenticação", expect.authenticated, result.authenticated)
    check_eq("encerramento", expect.ended, result.conversation_ended)
    check_eq("agente ativo", expect.active_agent, result.active_agent)
    check(
        "rota",
        route_ok,
        f"esperado={list(expect.route)}, obtido={list(telemetry.agent_route)}",
    )
    check_eq("guardrail", expect.guardrail_blocked, telemetry.guardrail_blocked)
    if expect.total_tokens is not None:
        check_eq("tokens", expect.total_tokens, telemetry.total_tokens)
    check_eq("confirmação", expect.confirmation_amount, confirmation_amount)
    check_eq(
        "valor para reenvio",
        expect.pending_requested_limit,
        state.get("pending_requested_limit"),
    )
    check(
        "pedidos no CSV",
        actual_requests == expect.requests,
        f"esperado={_request_summary(expect.requests)}, obtido={_request_summary(actual_requests)}",
    )
    check("CSVs preservados", not changed_files, f"alterados={changed_files}")
    check("resposta das ferramentas", not missing_tool_parts, f"ausentes={missing_tool_parts}")
    check("resposta sem PII", not leaked)

    if expect.reply_patterns:
        missing_patterns = [
            pattern
            for pattern in expect.reply_patterns
            if re.search(pattern, result.reply, re.IGNORECASE) is None
        ]
        check("padrões na resposta", not missing_patterns, f"ausentes={missing_patterns}")
    if expect.reply_not_contains:
        reply_lower = result.reply.casefold()
        forbidden_terms = [
            term for term in expect.reply_not_contains if term.casefold() in reply_lower
        ]
        check(
            "termos proibidos na resposta",
            not forbidden_terms,
            f"encontrados={forbidden_terms}",
        )
    if expect.auth_attempts is not None:
        check_eq(
            "tentativas de autenticação",
            expect.auth_attempts,
            state.get("auth_attempts", 0),
        )
    if expect.tools is not None:
        check_eq("ferramentas chamadas", list(expect.tools), list(telemetry.tools_called))
    if expect.client_limit is not None or expect.client_score is not None:
        cpf = state.get("client_cpf")
        client = clients.get(cpf) if cpf else None
        if expect.client_limit is not None:
            check_eq(
                "limite do cliente",
                expect.client_limit,
                client.limite_atual if client else None,
            )
        if expect.client_score is not None:
            check_eq("score do cliente", expect.client_score, client.score if client else None)
    if scripted is not None:
        missing_reply_parts = [part for part in expect.reply_contains if part not in result.reply]
        check("resposta do roteiro", not missing_reply_parts, f"ausentes={missing_reply_parts}")
        check("roteiro consumido", not scripted.responses, f"restantes={len(scripted.responses)}")
    return failures


def _run_scenario(
    scenario: EvalScenario,
    settings: Settings,
    *,
    live_model: BaseChatModel | None,
    observability: TurnObservability,
    scenario_id: str | None = None,
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
            route_ok = result.telemetry.agent_route == expect.route
            route_matches += route_ok
            leaked = contains_pii(result.reply, cpfs=cpfs, birth_dates=dates)
            pii_leaks += leaked
            changed_files = [
                name for name, data in before.items() if (work_dir / name).read_bytes() != data
            ]
            failures.extend(
                _collect_turn_failures(
                    index=index,
                    expect=expect,
                    result=result,
                    state=state,
                    route_ok=route_ok,
                    leaked=leaked,
                    tool_text=tool_text,
                    changed_files=changed_files,
                    clients=clients,
                    requests=requests,
                    scripted=scripted,
                )
            )
            if scripted is not None:
                scripted.responses.clear()

    return ScenarioEvalResult(
        id=scenario_id or scenario.id,
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
    scenario_ids: Sequence[str] | None = None,
    runs: int = 1,
) -> EvalReport:
    if mode not in ("offline", "live"):
        raise ValueError("Modo de avaliação inválido")
    if runs < 1:
        raise ValueError("O número de execuções deve ser maior ou igual a 1")
    scenarios = load_scenarios(scenarios_path)
    if scenario_ids:
        requested = tuple(dict.fromkeys(scenario_ids))
        by_id = {scenario.id: scenario for scenario in scenarios}
        missing = [item for item in requested if item not in by_id]
        if missing:
            raise ValueError(f"Cenários não encontrados: {', '.join(missing)}")
        scenarios = [by_id[item] for item in requested]
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
                for run_index in range(1, runs + 1):
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
                                scenario_id=f"{scenario.id}#{run_index}" if runs > 1 else None,
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

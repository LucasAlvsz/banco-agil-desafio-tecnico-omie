import logging
import socket
import subprocess
from collections.abc import Iterator

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from banco_agil.config import Settings
from banco_agil.domain.models import ExchangeQuote
from banco_agil.domain.repositories import ExchangeRateProvider
from banco_agil.domain.services import CreditService
from banco_agil.infrastructure.csv_repositories import (
    CsvClientRepository,
    CsvLimitRequestRepository,
    CsvScoreLimitRepository,
)


def pytest_configure(config):
    # Bloqueia rede também na coleta/importação, antes de carregar os SDKs.
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    patch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    patch.setenv("LITELLM_TELEMETRY", "False")
    patch.setenv("LANGCHAIN_TRACING_V2", "false")
    patch.setenv("LANGSMITH_TRACING", "false")

    def no_network(*args, **kwargs):
        raise AssertionError("Rede proibida no pytest")

    for name in ("connect", "connect_ex", "sendto"):
        patch.setattr(socket.socket, name, no_network)
    patch.setattr(socket, "getaddrinfo", no_network)
    patch.setattr(socket, "create_connection", no_network)

    def no_subprocess(*args, **kwargs):
        raise AssertionError("Subprocessos proibidos no pytest")

    patch.setattr(subprocess.Popen, "__init__", no_subprocess)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


@pytest.fixture
def isolated_logging(monkeypatch):
    loggers = (logging.getLogger(), logging.getLogger("banco_agil"))
    with monkeypatch.context() as patch:
        for logger in loggers:
            patch.setattr(logger, "level", logger.level)
            patch.setattr(logger, "handlers", logger.handlers.copy())
            for handler in logger.handlers:
                patch.setattr(handler, "filters", handler.filters.copy())
        yield
    for logger in loggers:
        logger.setLevel(logger.level)


class ScriptedChatModel(BaseChatModel):
    responses: list[Exception | AIMessage]
    calls: list[dict] = Field(default_factory=list)
    bindings: int = 0

    @property
    def _llm_type(self):
        return "scripted-offline"

    def bind_tools(self, tools, **kwargs):
        self.bindings += 1
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append({"messages": messages, "tools": kwargs.get("tools")})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


@pytest.fixture
def scripted_model():
    return ScriptedChatModel


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "clientes.csv").write_text(
        "cpf,nome,data_nascimento,limite_atual,score\n"
        "12345678901,Ana Souza,1990-05-14,2500.00,480\n",
        encoding="utf-8",
    )
    (tmp_path / "score_limite.csv").write_text("score_minimo,limite_maximo\n0,1000.00\n")
    (tmp_path / "solicitacoes_aumento_limite.csv").write_text(
        "cpf_cliente,data_hora_solicitacao,limite_atual,novo_limite_solicitado,status_pedido\n"
    )
    return tmp_path


@pytest.fixture
def credit_service(data_dir):
    return CreditService(
        CsvClientRepository(data_dir / "clientes.csv"),
        CsvScoreLimitRepository(data_dir / "score_limite.csv"),
        CsvLimitRequestRepository(data_dir / "solicitacoes_aumento_limite.csv"),
    )


@pytest.fixture
def exchange_provider() -> Iterator[ExchangeRateProvider]:
    unexpected_quote = False
    error = "Provedor de câmbio não esperado neste teste"

    class UnexpectedExchangeProvider:
        async def get_quote(self, currency: str = "USD") -> ExchangeQuote:
            nonlocal unexpected_quote
            unexpected_quote = True
            raise AssertionError(error)

    yield UnexpectedExchangeProvider()
    assert not unexpected_quote, error


@pytest.fixture
def chat_response():
    def make_response(**changes):
        return {
            "thread_id": "api-thread",
            "reply": "Resposta",
            "conversation_ended": False,
            "active_agent": "credito",
            "authenticated": True,
            "pending_confirmation": None,
            "telemetry": {"latency_ms": 5, "total_tokens": 7, "agent_route": ["credito"]},
            "langfuse_enabled": False,
        } | changes

    return make_response

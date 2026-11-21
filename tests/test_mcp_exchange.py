import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, ListToolsResult, TextContent
from pydantic import ValidationError

from banco_agil.agents.graph import build_graph, run_turn
from banco_agil.config import Settings
from banco_agil.domain.errors import DomainValidationError, ExchangeServiceError
from banco_agil.domain.models import ExchangeQuote
from banco_agil.infrastructure import mcp_exchange_client
from banco_agil.infrastructure.exchange_client import ExchangeRateClient
from banco_agil.infrastructure.mcp_exchange_client import (
    McpExchangeRateProvider,
    create_exchange_provider,
)
from banco_agil.mcp import exchange_server

TOOL = "consultar_cotacao_cambio"
PRIVATE = "123.456.789-01"
QUOTE = ExchangeQuote(
    currency_code="USD",
    currency_name="USD",
    bid=5.25,
    timestamp="2026-11-21 10:30:00",
    source="AwesomeAPI",
)


@pytest.fixture
def exchange_http():
    def provider(*, fallback=False, unavailable=False):
        def respond(request):
            if request.url.host == "economia.awesomeapi.com.br":
                assert request.url.path == "/json/last/USD-BRL"
                if not fallback and not unavailable:
                    return httpx.Response(
                        200, json={"USDBRL": {"bid": "5.25", "create_date": QUOTE.timestamp}}
                    )
                raise httpx.ReadTimeout("Timeout simulado", request=request)
            assert request.url.host == "api.frankfurter.dev"
            assert request.url.params == httpx.QueryParams(base="USD", symbols="BRL")
            if unavailable:
                return httpx.Response(503)
            return httpx.Response(200, json={"date": "2026-11-20", "rates": {"BRL": 5.17}})

        return ExchangeRateClient(transport=httpx.MockTransport(respond))

    return provider


async def test_mcp_schema_and_default_quote(exchange_http):
    server = exchange_server.create_exchange_mcp_server(exchange_http())
    async with Client(server) as client:
        (tool,) = (await client.list_tools()).tools
        assert tool.name == TOOL
        assert tool.input_schema["properties"] == {
            "moeda": {"default": "USD", "title": "Moeda", "type": "string"}
        }
        assert not tool.input_schema.get("required")
        result = await client.call_tool(TOOL, {})
    assert not result.is_error
    assert ExchangeQuote.model_validate(result.structured_content) == QUOTE


@pytest.mark.parametrize("fallback", [False, True])
async def test_provider_validates_mcp_quote(exchange_http, fallback):
    provider = McpExchangeRateProvider.in_memory(exchange_http(fallback=fallback))
    quote = await provider.get_quote("dólar")
    if fallback:
        assert quote == ExchangeQuote(
            currency_code="USD",
            currency_name="USD",
            bid=5.17,
            timestamp="2026-11-20",
            source="Frankfurter (BCE)",
            is_reference_rate=True,
        )
    else:
        assert quote == QUOTE


@pytest.mark.parametrize("fallback", [False, True])
def test_authenticated_graph_quotes_through_mcp(
    scripted_model, credit_service, exchange_http, fallback
):
    def tool_step(name, **args):
        return AIMessage(
            content="", tool_calls=[{"name": name, "args": args, "id": name, "type": "tool_call"}]
        )

    data = {"cpf": PRIVATE, "data_nascimento": "14/05/1990"}
    expected = "Frankfurter (BCE)" if fallback else "AwesomeAPI"
    expected_date = "2026-11-20" if fallback else QUOTE.timestamp
    model = scripted_model(
        responses=[
            tool_step("autenticar_cliente", **data),
            tool_step("redirecionar_para_cambio"),
            tool_step("consultar_cotacao", moeda="dólar"),
            AIMessage(
                content=f"Cotação de {expected} em {expected_date}. Dados: {PRIVATE}, 14/05/1990."
            ),
        ]
    )
    upstream = Mock(wraps=exchange_http(fallback=fallback))
    graph = build_graph(
        model, credit_service, exchange_provider=McpExchangeRateProvider.in_memory(upstream)
    )
    result = run_turn(graph, thread_id="exchange-mcp", user_message="Qual é a cotação do dólar?")
    state = graph.get_state({"configurable": {"thread_id": "exchange-mcp"}}).values
    (quote,) = [
        msg.text
        for msg in state["messages"]
        if isinstance(msg, ToolMessage) and msg.tool_call_id == "consultar_cotacao"
    ]
    assert result.authenticated and result.active_agent == "cambio"
    upstream.get_quote.assert_called_once_with("dólar")
    assert expected in quote and expected_date in quote
    assert expected_date in result.reply and PRIVATE not in result.reply
    assert "14/05/1990" not in result.reply


@pytest.mark.parametrize("currency", ["BRL", "", "moeda desconhecida", PRIVATE, {"cpf": PRIVATE}])
async def test_mcp_returns_safe_validation_error(exchange_http, currency):
    server = exchange_server.create_exchange_mcp_server(exchange_http())
    async with Client(server) as client:
        await client.list_tools()
        result = await client.call_tool(TOOL, {"moeda": currency})
    assert result.is_error
    (content,) = result.content
    assert isinstance(content, TextContent)
    assert "INVALID_CURRENCY:" in content.text
    assert PRIVATE not in content.text


@pytest.mark.parametrize("currency", ["BRL", PRIVATE])
async def test_provider_translates_validation_error_without_sending_pii(exchange_http, currency):
    provider = exchange_http()
    spy = Mock(wraps=provider)
    remote = McpExchangeRateProvider.in_memory(spy)
    with pytest.raises(DomainValidationError) as error:
        await remote.get_quote(currency)
    spy.get_quote.assert_called_once_with("BRL" if currency == "BRL" else "")
    assert PRIVATE not in str(error.value)


@pytest.mark.parametrize("unexpected", [False, True])
async def test_mcp_provider_failure_logs_only_exception_class(exchange_http, caplog, unexpected):
    class BrokenProvider:
        async def get_quote(self, currency="USD"):
            raise RuntimeError(PRIVATE)

    provider = BrokenProvider() if unexpected else exchange_http(unavailable=True)
    remote = McpExchangeRateProvider.in_memory(provider)
    with pytest.raises(ExchangeServiceError, match="Não foi possível consultar a cotação"):
        await remote.get_quote()

    error_type = "RuntimeError" if unexpected else "ExchangeServiceError"
    server_logs = [r for r in caplog.records if r.name == exchange_server.__name__]
    assert [r.getMessage() for r in server_logs] == [
        f"Falha no servidor MCP de câmbio ({error_type})"
    ]
    assert server_logs[0].exc_info is None
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("transport", ["inmemory", "stdio", "streamable_http"])
async def test_factory_and_transport_discovery(monkeypatch, exchange_http, transport):
    url = "http://mcp-exchange:8001/mcp"
    settings = Settings(
        mcp_exchange_transport=transport, mcp_exchange_url=url, exchange_timeout_seconds=3
    )
    # Use a real in-process MCP session for every mode behind the transport spy.
    server = exchange_server.create_exchange_mcp_server(exchange_http())
    client_factory = Mock(side_effect=lambda *args, **kwargs: Client(server))
    monkeypatch.setattr(mcp_exchange_client, "Client", client_factory)
    provider = create_exchange_provider(settings)
    assert await provider.get_quote() == QUOTE
    (endpoint,), kwargs = client_factory.call_args
    if transport == "inmemory":
        assert isinstance(endpoint, MCPServer)
        assert kwargs == {"read_timeout_seconds": None}
    elif transport == "stdio":
        assert isinstance(endpoint, StdioServerParameters)
        assert endpoint.command == sys.executable
        assert endpoint.args == ["-m", "banco_agil.mcp.exchange_server", "--timeout", "3.0"]
        assert kwargs == {"read_timeout_seconds": 6}
    else:
        assert endpoint == url
        assert kwargs == {"read_timeout_seconds": 6}


@pytest.mark.parametrize("url", [None, " ", "ftp://localhost", "http://localhost:0", "https://"])
def test_streamable_http_requires_http_url(url):
    with pytest.raises(ValidationError, match="MCP_EXCHANGE_URL|HTTP ou HTTPS"):
        Settings(mcp_exchange_transport="streamable_http", mcp_exchange_url=url)


@pytest.mark.parametrize("failure", ["session", "missing", "bad_json", "bad_shape", "no_quote"])
async def test_mcp_provider_handles_bad_session_or_response(monkeypatch, caplog, failure):
    provider = McpExchangeRateProvider.in_memory()
    if failure == "session":
        monkeypatch.setattr(mcp_exchange_client, "Client", Mock(side_effect=OSError(PRIVATE)))
    elif failure == "missing":

        async def list_tools(self):
            return ListToolsResult(tools=[])

        monkeypatch.setattr(Client, "list_tools", list_tools)
    else:
        responses = {
            "bad_json": CallToolResult(content=[TextContent(type="text", text=PRIVATE)]),
            "bad_shape": CallToolResult(content=[], structured_content={"bad": PRIVATE}),
            "no_quote": CallToolResult(content=[]),
        }

        async def call_tool(self, name, arguments):
            return responses[failure]

        monkeypatch.setattr(Client, "call_tool", call_tool)

    with pytest.raises(ExchangeServiceError) as error:
        await provider.get_quote()
    assert str(error.value) == "Não foi possível consultar a cotação no momento."
    assert PRIVATE not in caplog.text


async def test_mcp_accepts_json_text_result(monkeypatch):
    async def call_tool(self, name, arguments):
        assert name == TOOL and arguments == {"moeda": "USD"}
        return CallToolResult(content=[TextContent(type="text", text=QUOTE.model_dump_json())])

    monkeypatch.setattr(Client, "call_tool", call_tool)
    assert await McpExchangeRateProvider.in_memory().get_quote() == QUOTE


def test_stdio_cli_uses_settings(monkeypatch):
    server = Mock()
    factory = Mock(return_value=server)
    monkeypatch.setattr(exchange_server, "create_exchange_mcp_server", factory)
    monkeypatch.setattr(sys, "argv", ["exchange-server"])
    monkeypatch.setenv("EXCHANGE_TIMEOUT_SECONDS", "2.5")
    exchange_server.main()
    factory.assert_called_once_with(timeout_seconds=2.5)
    server.run.assert_called_once_with(transport="stdio")


def test_streamable_http_cli_keeps_dns_rebinding_protection(monkeypatch):
    server = Mock()
    factory = Mock(return_value=server)
    monkeypatch.setattr(exchange_server, "create_exchange_mcp_server", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "exchange-server",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",  # noqa: S104
            "--port",
            "8010",
            "--timeout",
            "3",
            "--allowed-host",
            "mcp-exchange",
            "--allowed-host",
            "mcp-exchange:8010",
        ],
    )
    exchange_server.main()
    factory.assert_called_once_with(timeout_seconds=3)
    kwargs = server.run.call_args.kwargs.copy()
    security = kwargs.pop("transport_security")
    assert kwargs == {
        "transport": "streamable-http",
        "host": "0.0.0.0",  # noqa: S104
        "port": 8010,
        "stateless_http": True,
        "json_response": True,
    }
    assert security.enable_dns_rebinding_protection
    assert security.allowed_hosts == [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "mcp-exchange",
        "mcp-exchange:8010",
    ]


def test_cli_rejects_invalid_timeout(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["exchange-server", "--timeout", "nan"])
    with pytest.raises(SystemExit) as error:
        exchange_server.main()
    assert error.value.code == 2
    assert "positivo e finito" in capsys.readouterr().err


def test_http_factory_rejects_url_when_settings_validation_was_bypassed():
    with pytest.raises(ValueError, match="MCP_EXCHANGE_URL"):
        create_exchange_provider(
            Settings.model_construct(
                mcp_exchange_transport="streamable_http", data_dir=Path("data")
            )
        )

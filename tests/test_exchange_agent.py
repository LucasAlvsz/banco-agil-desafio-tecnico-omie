import asyncio
import logging

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import ToolRuntime

from banco_agil.agents.graph import build_graph, run_turn
from banco_agil.agents.tools import build_exchange_tools
from banco_agil.domain.errors import ExchangeServiceError
from banco_agil.domain.models import ExchangeQuote
from banco_agil.domain.rules import normalize_currency_code

THREAD = "exchange-test"
CONFIG = {"configurable": {"thread_id": THREAD}}
CPF = "12345678901"
BIRTH_DATE = "14/05/1990"
QUOTE_TOOL = "consultar_cotacao"
PRIMARY = ExchangeQuote(
    currency_code="USD",
    currency_name="Dólar Americano",
    bid=5.25,
    ask=5.2711,
    timestamp="2026-11-21 10:30:00",
    source="AwesomeAPI",
)
REFERENCE = ExchangeQuote(
    currency_code="USD",
    currency_name="USD",
    bid=5.17,
    timestamp="2026-11-20",
    source="Frankfurter (BCE)",
    is_reference_rate=True,
)


class FakeExchangeProvider:
    def __init__(self, outcome=PRIMARY):
        self.outcome = outcome
        self.calls = []
        self.loop = None

    async def get_quote(self, currency="USD"):
        self.loop = asyncio.get_running_loop()
        self.calls.append(currency)
        normalize_currency_code(currency)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def tool_step(name, call_id, **args):
    return AIMessage(
        content="", tool_calls=[{"name": name, "id": call_id, "args": args, "type": "tool_call"}]
    )


@pytest.fixture
def exchange_graph(scripted_model, credit_service):
    def build(provider, *responses):
        model = scripted_model(
            responses=[
                tool_step("autenticar_cliente", "auth", cpf=CPF, data_nascimento=BIRTH_DATE),
                tool_step("redirecionar_para_cambio", "exchange"),
                *responses,
            ]
        )
        return build_graph(model, credit_service, exchange_provider=provider), model

    return build


def turn(graph, message="Quero saber a cotação."):
    return run_turn(graph, thread_id=THREAD, user_message=message)


def quote_message(graph):
    return next(
        message.text
        for message in graph.get_state(CONFIG).values["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == "quote"
    )


def invoke_quote_tool(tool, *, authenticated, cpf):
    runtime = ToolRuntime(
        state={"messages": [], "authenticated": authenticated, "client_cpf": cpf},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="quote",
        store=None,
    )
    return tool.invoke({"runtime": runtime}).update


@pytest.mark.parametrize(("authenticated", "cpf"), [(False, CPF), (True, None)])
def test_quote_tool_schema_and_gate(authenticated, cpf):
    provider = FakeExchangeProvider()
    (tool,) = build_exchange_tools(provider)
    schema = tool.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"moeda"}
    assert schema["properties"]["moeda"]["default"] == "USD"
    assert not schema.get("required")

    update = invoke_quote_tool(tool, authenticated=authenticated, cpf=cpf)
    assert provider.calls == []
    assert update["active_agent"] == "triagem"
    (message,) = update["messages"]
    assert message.text == "Autentique o cliente primeiro."
    assert message.tool_call_id == "quote"


@pytest.mark.parametrize(
    ("action", "args", "active_agent", "ended", "reply"),
    [
        ("voltar_para_triagem", {}, "triagem", False, "Em que posso ajudar?"),
        ("encerrar_atendimento", {"motivo": "A pedido do cliente"}, "cambio", True, "Até logo!"),
    ],
)
def test_authenticated_quote_and_next_action(
    exchange_graph, action, args, active_agent, ended, reply
):
    provider = FakeExchangeProvider()
    quotation = "A compra do dólar está em R$ 5,25. Posso ajudar em outro assunto?"
    graph, model = exchange_graph(
        provider,
        tool_step(QUOTE_TOOL, "quote", moeda="dólar"),
        AIMessage(content=quotation),
        tool_step(action, "next", **args),
        AIMessage(content=reply),
    )

    result = turn(graph, f"CPF {CPF}, nasci em {BIRTH_DATE}. Qual a cotação do dólar?")
    assert result.authenticated and result.active_agent == "cambio"
    assert result.reply == quotation and not result.conversation_ended
    assert quote_message(graph) == (
        "Dólar Americano (USD) em BRL. Compra: R$ 5,25. Venda: R$ 5,2711. "
        "Data e hora da cotação: 2026-11-21 10:30:00. Fonte: AwesomeAPI."
    )
    call = model.calls[-1]
    assert {tool["function"]["name"] for tool in call["tools"]} == {
        QUOTE_TOOL,
        "voltar_para_triagem",
        "encerrar_atendimento",
    }
    assert "taxa de referência diária" in call["messages"][0].text
    assert "data de referência" in call["messages"][0].text

    following = turn(graph, "Pode encerrar." if ended else "Quero falar de outro assunto.")
    assert following.authenticated and following.active_agent == active_agent
    assert following.conversation_ended is ended
    assert following.reply == reply
    assert provider.calls == ["dólar"]
    if ended:
        assert model.calls[-1]["tools"] is None


def test_reference_date_is_preserved_in_final_reply(exchange_graph):
    provider = FakeExchangeProvider(REFERENCE)
    reference = "A taxa de referência diária do BCE foi de R$ 5,17 em 2026-11-20."
    graph, _ = exchange_graph(
        provider,
        tool_step(QUOTE_TOOL, "quote"),
        AIMessage(content=f"{reference} Dados: {CPF}, {BIRTH_DATE}."),
    )

    result = turn(graph)
    assert result.authenticated and result.active_agent == "cambio"
    assert quote_message(graph) == (
        "USD em BRL. Taxa de referência diária: R$ 5,17. Data de referência: 2026-11-20. "
        "Fonte: Frankfurter (BCE)."
    )
    assert result.reply == f"{reference} Dados: [dado omitido], [dado omitido]."
    assert provider.calls == ["USD"]


@pytest.mark.parametrize(
    ("currency", "outcome", "guidance", "logged_type"),
    [
        ("BRL", PRIMARY, "O real (BRL) já é a moeda de referência.", None),
        (
            "moeda desconhecida",
            PRIMARY,
            "Informe um nome de moeda conhecido ou um código de três letras.",
            None,
        ),
        (
            "USD",
            ExchangeServiceError("detalhe sigiloso"),
            "A consulta de câmbio está temporariamente indisponível.",
            "ExchangeServiceError",
        ),
        (
            "USD",
            RuntimeError("detalhe sigiloso"),
            "A consulta de câmbio está temporariamente indisponível.",
            "RuntimeError",
        ),
    ],
)
def test_quote_errors_keep_conversation_open(
    exchange_graph, caplog, currency, outcome, guidance, logged_type
):
    provider = FakeExchangeProvider(outcome)
    graph, _ = exchange_graph(
        provider, tool_step(QUOTE_TOOL, "quote", moeda=currency), AIMessage(content=guidance)
    )

    result = turn(graph)
    assert result.authenticated and result.active_agent == "cambio"
    assert not result.conversation_ended
    assert quote_message(graph) == result.reply == guidance
    assert provider.calls == [currency]
    logs = [
        (record.levelno, record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == "banco_agil.agents.tools"
    ]
    assert logs == ([(logging.ERROR, logged_type, None)] if logged_type else [])


async def test_quote_runs_on_a_separate_loop_when_tool_is_invoked_from_async_code():
    provider = FakeExchangeProvider(PRIMARY.model_copy(update={"ask": None}))
    (tool,) = build_exchange_tools(provider)

    update = invoke_quote_tool(tool, authenticated=True, cpf=CPF)

    assert provider.calls == ["USD"]
    assert provider.loop is not None and provider.loop is not asyncio.get_running_loop()
    (message,) = update["messages"]
    assert "Compra: R$ 5,25." in message.text
    assert "Venda" not in message.text

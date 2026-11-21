import csv
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import ToolRuntime

from banco_agil.agents.graph import build_graph, resume_turn, run_turn
from banco_agil.agents.tools import build_interview_tools
from banco_agil.domain.errors import ClientNotFoundError, PersistenceError
from banco_agil.domain.models import Client

THREAD = "interview-test"
CONFIG = {"configurable": {"thread_id": THREAD}}
ANA = "12345678901"
CARLOS = "98765432100"
BIRTH_DATE = "1990-05-14"
CPF_INPUT = "123.456.789-01"
DATE_INPUT = "14/05/1990"
SCORE_TOOL = "calcular_e_atualizar_score"
ANSWERS = {
    "renda_mensal": 6000,
    "tipo_emprego": "formal",
    "despesas_fixas": 2000,
    "num_dependentes": 0,
    "tem_dividas": "não",
}


@pytest.fixture
def interview_files(data_dir):
    seeds = Path(__file__).resolve().parents[1] / "data"
    clients = data_dir / "clientes.csv"
    for path in (clients, data_dir / "score_limite.csv"):
        path.write_bytes((seeds / path.name).read_bytes())
    return clients, data_dir / "solicitacoes_aumento_limite.csv"


@pytest.fixture
def start_interview(scripted_model, credit_service, interview_files):
    def start(*responses):
        model = scripted_model(responses=list(responses))
        graph = build_graph(model, credit_service)
        graph.update_state(
            CONFIG,
            {
                "active_agent": "entrevista",
                "authenticated": True,
                "client_cpf": ANA,
                "client_birth_date": BIRTH_DATE,
                "messages": [AIMessage(content="Quais são seus dados financeiros?")],
            },
            as_node="credito",
        )
        return graph, model

    return start


def call(name, call_id, **args):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def tool_step(*calls):
    return AIMessage(content="", tool_calls=list(calls))


def turn(graph, message="Quero continuar a entrevista."):
    return run_turn(graph, thread_id=THREAD, user_message=message)


def tool_messages(graph):
    return {
        message.tool_call_id: message
        for message in graph.get_state(CONFIG).values["messages"]
        if isinstance(message, ToolMessage)
    }


def csv_rows(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def assert_private(*texts):
    for text in texts:
        assert all(value not in text for value in (ANA, CARLOS, BIRTH_DATE, CPF_INPUT, DATE_INPUT))


@pytest.mark.parametrize("authenticated,cpf", [(False, ANA), (True, None)])
def test_score_schema_and_authentication_gate(credit_service, monkeypatch, authenticated, cpf):
    recalculate = Mock()
    monkeypatch.setattr(credit_service, "recalculate_score", recalculate)
    (tool,) = build_interview_tools(credit_service)
    schema = tool.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == set(schema["required"]) == set(ANSWERS)
    runtime = ToolRuntime(
        state={"messages": [], "authenticated": authenticated, "client_cpf": cpf},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="gate",
        store=None,
    )

    result = tool.invoke({**ANSWERS, "runtime": runtime})

    recalculate.assert_not_called()
    assert result.update["active_agent"] == "triagem"
    (message,) = result.update["messages"]
    assert message.tool_call_id == "gate"
    assert message.text == "Autentique o cliente primeiro."
    assert_private(message.model_dump_json())


def test_score_updates_only_the_authenticated_client_and_hands_off_in_the_same_turn(
    start_interview, interview_files
):
    clients, requests = interview_files
    before = [Client.model_validate(row) for row in csv_rows(clients)]
    graph, model = start_interview(
        tool_step(call(SCORE_TOOL, "score", **ANSWERS, cpf=CARLOS)),
        AIMessage(content=f"Seu novo score é 590. ({CPF_INPUT}, {DATE_INPUT})"),
    )

    result = turn(graph)

    assert result.reply == "Seu novo score é 590. ([dado omitido], [dado omitido])"
    assert result.active_agent == graph.get_state(CONFIG).values["active_agent"] == "credito"
    assert result.pending_confirmation is None
    score_message = tool_messages(graph)["score"]
    assert "Novo score: 590." in score_message.text
    assert model.calls[1]["messages"][-1] == score_message
    assert "solicitar_aumento_limite" in {
        tool["function"]["name"] for tool in model.calls[1]["tools"]
    }
    assert_private(score_message.model_dump_json(), result.model_dump_json())
    assert [Client.model_validate(row) for row in csv_rows(clients)] == [
        client.model_copy(update={"score": 590}) if client.cpf == ANA else client
        for client in before
    ]
    assert csv_rows(requests) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("renda_mensal", -1),
        ("despesas_fixas", float("nan")),
        ("tipo_emprego", CPF_INPUT),
        ("num_dependentes", -1),
        ("tem_dividas", DATE_INPUT),
    ],
)
def test_invalid_indicators_keep_the_interview_open(
    start_interview, interview_files, caplog, field, value
):
    clients, _ = interview_files
    before = clients.read_bytes()
    graph, _ = start_interview(
        tool_step(call(SCORE_TOOL, "invalid", **(ANSWERS | {field: value}))),
        AIMessage(content="Pode corrigir esse dado?"),
    )

    result = turn(graph)

    assert result.active_agent == graph.get_state(CONFIG).values["active_agent"] == "entrevista"
    message = tool_messages(graph)["invalid"]
    assert message.text.startswith("Revise os dados da entrevista:")
    assert_private(message.model_dump_json(), result.model_dump_json(), caplog.text)
    assert clients.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("renda_mensal", True),
        ("despesas_fixas", False),
        ("num_dependentes", True),
        ("tem_dividas", 1),
    ],
)
def test_score_tool_rejects_coercion(start_interview, credit_service, monkeypatch, field, value):
    recalculate = Mock()
    monkeypatch.setattr(credit_service, "recalculate_score", recalculate)
    graph, _ = start_interview(
        tool_step(call(SCORE_TOOL, "invalid-type", **(ANSWERS | {field: value}))),
        AIMessage(content="Pode corrigir esse dado?"),
    )

    assert turn(graph).active_agent == "entrevista"
    assert (
        tool_messages(graph)["invalid-type"].text
        == "Revise os dados informados para esta operação."
    )
    recalculate.assert_not_called()


@pytest.mark.parametrize(
    "method,error", [("update", PersistenceError), ("get", ClientNotFoundError)]
)
def test_repository_errors_are_controlled_and_private(
    start_interview, interview_files, credit_service, monkeypatch, caplog, method, error
):
    clients, _ = interview_files
    before = clients.read_bytes()
    graph, _ = start_interview(
        tool_step(call(SCORE_TOOL, "fail", **ANSWERS)),
        AIMessage(content="Por favor, tente em instantes."),
    )
    monkeypatch.setattr(
        credit_service.clients, method, Mock(side_effect=error(f"{ANA} {BIRTH_DATE}"))
    )

    result = turn(graph)

    assert result.active_agent == graph.get_state(CONFIG).values["active_agent"] == "entrevista"
    message = tool_messages(graph)["fail"]
    assert message.text == "A atualização do score está temporariamente indisponível."
    assert caplog.record_tuples == [("banco_agil.agents.tools", logging.ERROR, error.__name__)]
    assert caplog.records[0].exc_info is None
    assert_private(message.model_dump_json(), result.model_dump_json(), caplog.text)
    assert clients.read_bytes() == before


def test_rejected_request_interview_and_confirmed_resubmission(
    scripted_model, credit_service, interview_files, monkeypatch
):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return cls(2026, 11, 21, 12, tzinfo=UTC)

    monkeypatch.setattr("banco_agil.domain.rules.datetime", FixedDatetime)
    clients, requests = interview_files
    original_clients = clients.read_bytes()
    offer = "Quer fazer uma entrevista para reavaliar seu score?"
    resubmit = "Seu novo score é 590. Quer reenviar o pedido de R$ 6.000 ou informar outro valor?"
    model = scripted_model(
        responses=[
            tool_step(
                call("autenticar_cliente", "auth", cpf=CPF_INPUT, data_nascimento=DATE_INPUT)
            ),
            tool_step(call("redirecionar_para_credito", "credit")),
            tool_step(call("solicitar_aumento_limite", "first", novo_limite=6000)),
            AIMessage(content=offer),
            tool_step(call("redirecionar_para_entrevista", "interview")),
            AIMessage(content="Quais são sua renda mensal e suas despesas fixas?"),
            tool_step(call(SCORE_TOOL, "score", **ANSWERS)),
            AIMessage(content=resubmit),
            tool_step(call("solicitar_aumento_limite", "second", novo_limite=6000)),
            AIMessage(content="Pedido aprovado. Seu novo limite é de R$ 6.000."),
        ]
    )
    graph = build_graph(model, credit_service)

    initial = turn(
        graph, f"Meu CPF é {CPF_INPUT}, nasci em {DATE_INPUT} e quero R$ 6.000 de limite."
    )
    assert initial.authenticated and initial.active_agent == "credito"
    assert initial.pending_confirmation.novo_limite_solicitado == 6000
    assert csv_rows(requests) == []
    assert clients.read_bytes() == original_clients

    rejected = resume_turn(graph, thread_id=THREAD, approved=True)
    assert rejected.reply == offer
    assert rejected.pending_confirmation is None
    (first_request,) = csv_rows(requests)
    assert first_request["status_pedido"] == "rejeitado"
    assert float(first_request["novo_limite_solicitado"]) == 6000
    original_requests = requests.read_bytes()
    assert clients.read_bytes() == original_clients
    assert graph.get_state(CONFIG).values["pending_requested_limit"] == 6000

    interview = turn(graph, "Sim, quero fazer a entrevista.")
    assert interview.active_agent == "entrevista"
    assert {tool["function"]["name"] for tool in model.calls[-1]["tools"]} == {
        SCORE_TOOL,
        "voltar_para_triagem",
        "encerrar_atendimento",
    }
    assert clients.read_bytes() == original_clients

    score = turn(
        graph, "Ganho R$ 6.000, sou CLT, gasto R$ 2.000 e não tenho dependentes nem dívidas."
    )
    assert score.reply == resubmit and score.active_agent == "credito"
    assert score.pending_confirmation is None
    assert graph.get_state(CONFIG).values["pending_requested_limit"] == 6000
    credit_prompt = model.calls[-1]["messages"][0].text
    assert "R$ 6000.00" in credit_prompt
    assert "informe o novo score" in credit_prompt
    assert "após o cliente confirmar" in credit_prompt
    updated = credit_service.get_client(ANA)
    assert (updated.score, updated.limite_atual) == (590, 2500)
    assert requests.read_bytes() == original_requests
    scored_clients = clients.read_bytes()

    confirmation = turn(graph, "Sim, quero reenviar o pedido de R$ 6.000.")
    pending = confirmation.pending_confirmation
    assert (pending.limite_atual, pending.novo_limite_solicitado) == (2500, 6000)
    assert clients.read_bytes() == scored_clients
    assert requests.read_bytes() == original_requests

    approved = resume_turn(graph, thread_id=THREAD, approved=True)
    assert approved.reply == "Pedido aprovado. Seu novo limite é de R$ 6.000."
    assert approved.pending_confirmation is None
    assert graph.get_state(CONFIG).values["pending_requested_limit"] is None
    assert csv_rows(requests) == [first_request, first_request | {"status_pedido": "aprovado"}]
    updated = credit_service.get_client(ANA)
    assert (updated.score, updated.limite_atual) == (590, 6000)
    assert not model.responses
    assert_private(
        *(
            result.model_dump_json()
            for result in (initial, rejected, interview, score, confirmation, approved)
        )
    )
    assert_private(*(message.model_dump_json() for message in tool_messages(graph).values()))


@pytest.mark.parametrize("closed", [False, True])
def test_client_can_leave_mid_interview_without_changing_the_score(
    start_interview, interview_files, closed
):
    clients, requests = interview_files
    before = clients.read_bytes()
    exit_call = (
        call("encerrar_atendimento", "exit", motivo="Pedido do cliente")
        if closed
        else call("voltar_para_triagem", "exit")
    )
    graph, model = start_interview(
        AIMessage(content="Quais são suas despesas fixas?"),
        tool_step(exit_call),
        AIMessage(content="Até logo!" if closed else "Como posso ajudar?"),
    )

    assert turn(graph, "Ganho R$ 6.000.").active_agent == "entrevista"
    result = turn(graph, "Quero encerrar." if closed else "Quero mudar de assunto.")

    assert result.active_agent == ("entrevista" if closed else "triagem")
    assert result.authenticated and result.conversation_ended is closed
    assert (model.calls[-1]["tools"] is None) is closed
    assert clients.read_bytes() == before
    assert csv_rows(requests) == []


@pytest.mark.parametrize(
    "batch,agent",
    [
        (("score", "back"), "credito"),
        (("back", "score"), "triagem"),
        (("score", "duplicate"), "credito"),
        (("score", "back", "close"), "entrevista"),
    ],
)
def test_parallel_calls_apply_only_the_selected_transition(
    start_interview, interview_files, credit_service, monkeypatch, batch, agent
):
    clients, _ = interview_files
    before = clients.read_bytes()
    recalculate = Mock(wraps=credit_service.recalculate_score)
    monkeypatch.setattr(credit_service, "recalculate_score", recalculate)
    calls = {
        "score": call(SCORE_TOOL, "score", **ANSWERS),
        "duplicate": call(SCORE_TOOL, "duplicate", **(ANSWERS | {"renda_mensal": 0})),
        "back": call("voltar_para_triagem", "back"),
        "close": call("encerrar_atendimento", "close", motivo="Pedido do cliente"),
    }
    graph, _ = start_interview(
        tool_step(*(calls[call_id] for call_id in batch)), AIMessage(content="Tudo certo.")
    )

    result = turn(graph)

    assert result.reply == "Tudo certo."
    assert result.active_agent == agent
    assert result.conversation_ended is ("close" in batch)
    selected = "close" if result.conversation_ended else batch[0]
    assert {call_id: message.status for call_id, message in tool_messages(graph).items()} == {
        call_id: "success" if call_id == selected else "error" for call_id in batch
    }
    if selected == "score":
        recalculate.assert_called_once_with(ANA, **ANSWERS)
        assert credit_service.get_client(ANA).score == 590
    else:
        recalculate.assert_not_called()
        assert clients.read_bytes() == before

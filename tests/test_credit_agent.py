import csv
import json
import logging
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import ValidationError

from banco_agil.agents.graph import (
    ENDED_REPLY,
    ERROR_REPLY,
    NO_CONFIRMATION_REPLY,
    PENDING_REPLY,
    build_graph,
    resume_turn,
    run_turn,
)
from banco_agil.agents.tools import build_credit_tools
from banco_agil.domain.errors import ClientNotFoundError, DomainValidationError, PersistenceError

THREAD = "credit-test"
CONFIG = {"configurable": {"thread_id": THREAD}}
ANA = "12345678901"
CARLOS = "98765432100"
BIRTH_DATE = "1990-05-14"
PRIVATE_VALUES = (ANA, CARLOS, BIRTH_DATE, "123.456.789-01", "14/05/1990")
TIMESTAMP = datetime(2026, 11, 21, 12, tzinfo=UTC)


@pytest.fixture
def credit_files(data_dir, monkeypatch):
    clients = data_dir / "clientes.csv"
    clients.write_text(
        "cpf,nome,data_nascimento,limite_atual,score\n"
        f"{ANA},Ana Souza,{BIRTH_DATE},2500.00,420\n"
        f"{CARLOS},Carlos Mendes,1985-04-10,5000.00,720\n"
    )
    (data_dir / "score_limite.csv").write_text(
        "score_minimo,limite_maximo\n0,1000.00\n300,3000.00\n650,15000.00\n"
    )

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return TIMESTAMP

    monkeypatch.setattr("banco_agil.domain.rules.datetime", FixedDatetime)
    return clients, data_dir / "solicitacoes_aumento_limite.csv"


@pytest.fixture
def credit_session(scripted_model, credit_service, credit_files):
    def start(*responses, cpf=ANA):
        model = scripted_model(responses=list(responses))
        graph = build_graph(model, credit_service)
        graph.update_state(
            CONFIG,
            {
                "active_agent": "credito",
                "authenticated": True,
                "client_cpf": cpf,
                "client_birth_date": credit_service.get_client(cpf).data_nascimento,
                "messages": [AIMessage(content="Como posso ajudar com seu crédito?")],
            },
            as_node="triagem",
        )
        return graph, model

    return start


def call(name, call_id="credit", **args):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def tool_step(*calls):
    return AIMessage(content="", tool_calls=list(calls))


def increase(amount):
    return tool_step(call("solicitar_aumento_limite", novo_limite=amount))


def turn(graph, message="Quero tratar do meu limite."):
    return run_turn(graph, thread_id=THREAD, user_message=message)


def last_tool(graph):
    return next(
        message
        for message in reversed(graph.get_state(CONFIG).values["messages"])
        if isinstance(message, ToolMessage)
    )


def csv_bytes(files):
    return tuple(path.read_bytes() for path in files)


def requests(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def assert_private(text):
    assert all(value not in text for value in PRIVATE_VALUES)


@pytest.mark.parametrize("name", ["consultar_limite", "solicitar_aumento_limite"])
def test_credit_tool_schema_and_unauthenticated_gate(credit_service, monkeypatch, name):
    def fail(*args):
        pytest.fail("Ferramenta sem autenticação acessou o serviço de crédito")

    monkeypatch.setattr(credit_service, "get_client", fail)
    tool = next(tool for tool in build_credit_tools(credit_service) if tool.name == name)
    args = {"novo_limite": 6000} if name == "solicitar_aumento_limite" else {}
    assert set(tool.tool_call_schema.model_json_schema().get("properties", {})) == set(args)
    runtime = ToolRuntime(
        state={"messages": [], "authenticated": False, "client_cpf": ANA},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="blocked",
        store=None,
    )
    result = tool.invoke({**args, "runtime": runtime})
    assert result.update["active_agent"] == "triagem"
    assert result.update["messages"][0].content == "Autentique o cliente primeiro."


def test_consultation_reads_the_latest_limit_and_redacts_reply(credit_session, credit_files):
    graph, _ = credit_session(
        tool_step(call("consultar_limite", cpf=CARLOS)),
        AIMessage(content=f"Seu limite é R$ 2700.00 ({ANA}, {BIRTH_DATE})."),
    )
    clients, _ = credit_files
    clients.write_text(clients.read_text().replace("2500.00", "2700.00"))
    original = csv_bytes(credit_files)

    result = turn(graph)

    assert result.reply == "Seu limite é R$ 2700.00 ([dado omitido], [dado omitido])."
    assert result.pending_confirmation is None
    assert last_tool(graph).content == "Limite atual: R$ 2700.00."
    assert_private(last_tool(graph).model_dump_json())
    assert csv_bytes(credit_files) == original


@pytest.mark.parametrize("error", [PersistenceError, ClientNotFoundError])
@pytest.mark.parametrize(
    ("name", "args", "reply"),
    [
        ("consultar_limite", {}, "A consulta de limite está temporariamente indisponível."),
        (
            "solicitar_aumento_limite",
            {"novo_limite": 6000},
            "O pedido de aumento está temporariamente indisponível.",
        ),
    ],
)
def test_read_failure_is_controlled_and_private(
    credit_session, credit_files, credit_service, monkeypatch, caplog, error, name, args, reply
):
    graph, _ = credit_session(tool_step(call(name, **args)), AIMessage(content=reply))
    original = csv_bytes(credit_files)

    def fail(*args):
        raise error(f"{ANA} {BIRTH_DATE}")

    monkeypatch.setattr(credit_service, "get_client", fail)
    assert turn(graph).reply == last_tool(graph).content == reply
    assert not graph.get_state(CONFIG).interrupts
    assert csv_bytes(credit_files) == original
    assert caplog.record_tuples == [("banco_agil.agents.tools", logging.ERROR, error.__name__)]
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("amount", [2500, 100, 0, -1, float("nan"), float("inf")])
def test_invalid_limit_does_not_pause_or_persist(credit_session, credit_files, amount):
    graph, _ = credit_session(increase(amount), AIMessage(content="Informe um valor maior."))
    original = csv_bytes(credit_files)

    assert turn(graph).pending_confirmation is None
    assert not graph.get_state(CONFIG).interrupts
    assert json.loads(last_tool(graph).text) == {
        "erro": "O novo limite precisa ser maior que o limite atual.",
        "limite_atual": 2500.0,
    }
    assert_private(last_tool(graph).model_dump_json())
    assert csv_bytes(credit_files) == original


def test_pending_turn_is_preserved_until_canceled(
    credit_session, credit_files, credit_service, monkeypatch
):
    process = Mock(wraps=credit_service.process_limit_increase)
    monkeypatch.setattr(credit_service, "process_limit_increase", process)
    graph, model = credit_session(increase(6000), AIMessage(content="Pedido cancelado."))
    original = csv_bytes(credit_files)

    initial = turn(graph)
    pending = initial.pending_confirmation
    assert initial.reply == pending.message
    assert (pending.limite_atual, pending.novo_limite_solicitado) == (2500, 6000)
    with pytest.raises(ValidationError, match="frozen"):
        pending.novo_limite_solicitado = 9000
    snapshot = graph.get_state(CONFIG)
    assert [item.value for item in snapshot.interrupts] == [
        {"action": "confirm_limit_increase", **pending.model_dump()}
    ]
    assert_private(initial.model_dump_json())
    assert_private(json.dumps(snapshot.interrupts[0].value))
    assert csv_bytes(credit_files) == original

    blocked = turn(graph, "Confirmo por mensagem.")
    assert blocked.reply == PENDING_REPLY
    assert blocked.pending_confirmation == pending
    assert graph.get_state(CONFIG) == snapshot
    assert len(model.calls) == 1
    assert resume_turn(graph, thread_id="outra-sessao", approved=True).reply == (
        NO_CONFIRMATION_REPLY
    )

    result = resume_turn(graph, thread_id=THREAD, approved=False)
    assert result.reply == "Pedido cancelado."
    assert "cancelado" in last_tool(graph).text
    assert result.pending_confirmation is None
    assert not graph.get_state(CONFIG).interrupts
    assert csv_bytes(credit_files) == original
    process.assert_not_called()
    assert resume_turn(graph, thread_id=THREAD, approved=True).reply == NO_CONFIRMATION_REPLY


@pytest.mark.parametrize("decision", ["sim", {"approved": 1}, {"approved": "true"}, {"ok": True}])
def test_resume_requires_a_boolean_true(credit_session, credit_files, decision):
    graph, _ = credit_session(increase(6000), AIMessage(content="Pedido cancelado."))
    original = csv_bytes(credit_files)
    assert turn(graph).pending_confirmation is not None

    graph.invoke(Command(resume=decision), CONFIG)

    assert "cancelado" in last_tool(graph).text
    assert csv_bytes(credit_files) == original


def test_approval_writes_once_and_updates_the_limit(
    credit_session, credit_service, credit_files, monkeypatch
):
    process = Mock(wraps=credit_service.process_limit_increase)
    monkeypatch.setattr(credit_service, "process_limit_increase", process)
    graph, _ = credit_session(
        tool_step(
            call("solicitar_aumento_limite", novo_limite=15000),
            call("solicitar_aumento_limite", "duplicate", novo_limite=8000),
        ),
        AIMessage(content="Pedido aprovado."),
        tool_step(call("consultar_limite")),
        AIMessage(content="Seu limite é R$ 15000.00."),
        cpf=CARLOS,
    )
    graph.update_state(CONFIG, {"pending_requested_limit": 12000.0}, as_node="triagem")
    original = csv_bytes(credit_files)

    pending = turn(graph).pending_confirmation
    assert (pending.limite_atual, pending.novo_limite_solicitado) == (5000, 15000)
    assert len(graph.get_state(CONFIG).interrupts) == 1
    assert csv_bytes(credit_files) == original
    process.assert_not_called()

    result = resume_turn(graph, thread_id=THREAD, approved=True)
    assert result.reply == "Pedido aprovado."
    assert result.pending_confirmation is None
    state = graph.get_state(CONFIG).values
    assert state["pending_requested_limit"] is None
    outputs = [message for message in state["messages"] if isinstance(message, ToolMessage)]
    assert outputs[0].text == "Pedido aprovado. Novo limite vigente: R$ 15000.00."
    assert outputs[1].status == "error"
    assert_private(outputs[0].model_dump_json())
    assert credit_service.get_client(CARLOS).limite_atual == 15000
    assert requests(credit_files[1]) == [
        {
            "cpf_cliente": CARLOS,
            "data_hora_solicitacao": TIMESTAMP.isoformat(),
            "limite_atual": "5000.0",
            "novo_limite_solicitado": "15000.0",
            "status_pedido": "aprovado",
        }
    ]
    assert resume_turn(graph, thread_id=THREAD, approved=True).reply == NO_CONFIRMATION_REPLY
    process.assert_called_once_with(CARLOS, 15000)
    assert turn(graph, "Qual é meu limite?").reply == "Seu limite é R$ 15000.00."
    assert last_tool(graph).content == "Limite atual: R$ 15000.00."


def test_rejection_preserves_the_amount_and_hands_off_to_interview(credit_session, credit_files):
    offer = "Pedido rejeitado. Quer fazer uma entrevista para reavaliar seu score?"
    graph, model = credit_session(
        increase(6000),
        AIMessage(content=offer),
        tool_step(call("redirecionar_para_entrevista")),
        AIMessage(content="Qual é sua renda mensal?"),
    )
    original = csv_bytes(credit_files)
    assert turn(graph).pending_confirmation is not None
    assert csv_bytes(credit_files) == original

    result = resume_turn(graph, thread_id=THREAD, approved=True)
    assert result.reply == offer
    assert result.active_agent == "credito"
    assert result.pending_confirmation is None
    assert "rejeitado" in last_tool(graph).text and "entrevista" in last_tool(graph).text
    assert_private(last_tool(graph).model_dump_json())
    assert credit_files[0].read_bytes() == original[0]
    rows = requests(credit_files[1])
    assert len(rows) == 1
    assert rows[0]["status_pedido"] == "rejeitado"
    assert float(rows[0]["novo_limite_solicitado"]) == 6000
    assert graph.get_state(CONFIG).values["pending_requested_limit"] == 6000
    assert "6000.00" in model.calls[1]["messages"][0].text

    result = turn(graph, "Sim, quero fazer a entrevista.")
    assert result.active_agent == "entrevista" and result.authenticated
    assert result.reply == "Qual é sua renda mensal?"
    assert "Conduza a entrevista de crédito" in model.calls[3]["messages"][0].text
    assert {tool["function"]["name"] for tool in model.calls[3]["tools"]} == {
        "calcular_e_atualizar_score",
        "voltar_para_triagem",
        "encerrar_atendimento",
    }
    assert graph.get_state(CONFIG).values["pending_requested_limit"] == 6000


def test_limit_is_revalidated_when_resuming(credit_session, credit_service, credit_files):
    graph, _ = credit_session(increase(6000), AIMessage(content="Seu limite foi atualizado."))
    assert turn(graph).pending_confirmation is not None
    client = credit_service.get_client(ANA)
    credit_service.clients.update(client.model_copy(update={"limite_atual": 6500}))
    updated = csv_bytes(credit_files)

    result = resume_turn(graph, thread_id=THREAD, approved=True)

    assert result.pending_confirmation is None
    assert json.loads(last_tool(graph).text)["limite_atual"] == 6500
    assert csv_bytes(credit_files) == updated


@pytest.mark.parametrize("error", [DomainValidationError, ClientNotFoundError, PersistenceError])
def test_write_failure_is_controlled_and_private(
    credit_session, credit_service, credit_files, monkeypatch, caplog, error
):
    graph, _ = credit_session(increase(6000), AIMessage(content="Tente novamente em instantes."))
    original = csv_bytes(credit_files)

    def fail(*args):
        raise error(f"{ANA} {BIRTH_DATE}")

    monkeypatch.setattr(credit_service, "process_limit_increase", fail)
    assert turn(graph).pending_confirmation is not None
    result = resume_turn(graph, thread_id=THREAD, approved=True)
    assert result.reply == "Tente novamente em instantes."
    assert result.pending_confirmation is None
    assert last_tool(graph).content == "Não foi possível concluir o pedido de aumento de limite."
    assert caplog.record_tuples == [("banco_agil.agents.tools", logging.ERROR, error.__name__)]
    assert caplog.records[0].exc_info is None
    assert csv_bytes(credit_files) == original


def test_provider_failure_after_confirmation_does_not_replay_the_write(
    credit_session, credit_files, caplog
):
    graph, _ = credit_session(increase(6000), RuntimeError(f"{ANA} {BIRTH_DATE}"))
    assert turn(graph).pending_confirmation is not None

    result = resume_turn(graph, thread_id=THREAD, approved=True)
    assert result.reply == ERROR_REPLY
    assert result.pending_confirmation is None
    assert resume_turn(graph, thread_id=THREAD, approved=True).reply == NO_CONFIRMATION_REPLY
    assert len(requests(credit_files[1])) == 1
    assert_private(caplog.text)


def test_resume_closed_conversation_does_not_invoke_the_graph(credit_session):
    graph, model = credit_session(
        tool_step(call("encerrar_atendimento", motivo="A pedido do cliente")),
        AIMessage(content="Até logo!"),
    )
    assert turn(graph).conversation_ended
    result = resume_turn(graph, thread_id=THREAD, approved=True)
    assert result.reply == ENDED_REPLY
    assert result.conversation_ended and result.pending_confirmation is None
    assert len(model.calls) == 2

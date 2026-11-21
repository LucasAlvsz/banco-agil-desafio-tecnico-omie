import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import ToolNode

from banco_agil.agents.graph import ENDED_REPLY, build_graph, run_turn
from banco_agil.domain.errors import PersistenceError

CONFIG = {"configurable": {"thread_id": "triage-test"}}
CPF = "123.456.789-01"
BIRTH_DATE = "14/05/1990"
TRIAGE_TOOLS = {
    "autenticar_cliente",
    "redirecionar_para_credito",
    "redirecionar_para_cambio",
    "encerrar_atendimento",
}
SPECIALIST_TOOLS = {"voltar_para_triagem", "encerrar_atendimento"}
CREDIT_TOOLS = SPECIALIST_TOOLS | {
    "consultar_limite",
    "solicitar_aumento_limite",
    "redirecionar_para_entrevista",
}


def call(name, call_id, **args):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def auth(call_id="auth", *, cpf=CPF, date=BIRTH_DATE):
    return call("autenticar_cliente", call_id, cpf=cpf, data_nascimento=date)


def tool_step(*calls):
    return AIMessage(content="", tool_calls=list(calls))


def turn(graph, message="Pode continuar?"):
    return run_turn(graph, thread_id=CONFIG["configurable"]["thread_id"], user_message=message)


def checkpoint(graph):
    return graph.get_state(CONFIG).values


def tool_messages(state):
    return [message for message in state["messages"] if isinstance(message, ToolMessage)]


def assert_no_personal_data(text):
    for value in (CPF, "12345678901", BIRTH_DATE, "1990-05-14"):
        assert value not in text


def test_physical_tool_isolation_and_schema(scripted_model, credit_service):
    model = scripted_model(
        responses=[
            tool_step(auth()),
            tool_step(call("redirecionar_para_credito", "credit")),
            tool_step(call("voltar_para_triagem", "back")),
            tool_step(call("redirecionar_para_cambio", "exchange")),
            AIMessage(content="Consultas de câmbio ainda estão indisponíveis."),
        ]
    )
    graph = build_graph(model, credit_service)
    assert turn(graph).active_agent == "cambio"

    nodes = graph.get_graph().nodes
    expected = {
        "triagem_tools": TRIAGE_TOOLS,
        "credito_tools": CREDIT_TOOLS,
        "entrevista_tools": SPECIALIST_TOOLS | {"calcular_e_atualizar_score"},
        "cambio_tools": SPECIALIST_TOOLS,
    }
    for name, tools in expected.items():
        node = nodes[name].data
        assert isinstance(node, ToolNode)
        assert set(node.tools_by_name) == tools
    assert nodes["credito_tools"].data is not nodes["cambio_tools"].data
    assert "entrevista" in nodes

    expected_arguments = {
        "autenticar_cliente": {"cpf", "data_nascimento"},
        "encerrar_atendimento": {"motivo"},
        "solicitar_aumento_limite": {"novo_limite"},
    }
    for model_call, tools in zip(
        model.calls,
        [TRIAGE_TOOLS, TRIAGE_TOOLS, CREDIT_TOOLS, TRIAGE_TOOLS, SPECIALIST_TOOLS],
        strict=True,
    ):
        definitions = [tool["function"] for tool in model_call["tools"]]
        assert {definition["name"] for definition in definitions} == tools
        for definition in definitions:
            properties = definition["parameters"].get("properties", {})
            assert set(properties) == expected_arguments.get(definition["name"], set())


def test_success_resets_attempts_and_reauthentication_preserves_identity(
    scripted_model, credit_service, monkeypatch
):
    model = scripted_model(
        responses=[
            tool_step(auth("mismatch", date="15/05/1990")),
            AIMessage(content="Confira seus dados."),
            tool_step(auth()),
            AIMessage(content="Olá, Ana Souza!"),
            tool_step(auth("repeat", cpf="98765432100")),
            AIMessage(content="Como posso ajudar?"),
        ]
    )
    graph = build_graph(model, credit_service)
    assert not turn(graph).authenticated
    assert checkpoint(graph)["auth_attempts"] == 1
    result = turn(graph, f"{CPF} e {BIRTH_DATE}")
    assert result.authenticated and result.active_agent == "triagem"
    assert result.reply == "Olá, Ana Souza!"
    assert "client_cpf" not in result.model_dump()
    assert "client_birth_date" not in result.model_dump()
    assert_no_personal_data(result.model_dump_json())
    state = checkpoint(graph)
    assert state["client_cpf"] == "12345678901"
    assert state["client_name"] == "Ana Souza"
    assert state["client_birth_date"] == "1990-05-14"
    assert state["auth_attempts"] == 0
    assert tool_messages(state)[-1].content == "Cliente autenticado: Ana Souza."
    assert "autenticar_cliente" in model.calls[2]["messages"][0].text
    assert "Ana Souza" in model.calls[3]["messages"][0].text

    def unexpected_auth(*args):
        pytest.fail("Sessão autenticada não deve consultar o cadastro novamente")

    monkeypatch.setattr(credit_service, "authenticate", unexpected_auth)
    assert turn(graph).authenticated
    state = checkpoint(graph)
    assert state["client_cpf"] == "12345678901"
    assert state["client_name"] == "Ana Souza"
    assert state["auth_attempts"] == 0
    assert tool_messages(state)[-1].content == "O cliente já está autenticado."


def test_invalid_formats_do_not_consume_attempts(scripted_model, credit_service):
    inputs = [
        {"cpf": "1234567890"},
        {"date": "31/02/1990"},
        {"cpf": "CPF " + CPF},
        {"date": "maio de 1990"},
        {"cpf": ""},
    ]
    responses = []
    for index, values in enumerate(inputs):
        responses += [
            tool_step(auth(str(index), **values)),
            AIMessage(content="Confira o formato dos dados."),
        ]
    graph = build_graph(scripted_model(responses=responses), credit_service)

    for _ in inputs:
        result = turn(graph)
        assert not result.authenticated and not result.conversation_ended
        state = checkpoint(graph)
        assert state.get("auth_attempts", 0) == 0
        assert tool_messages(state)[-1].content == (
            "Informe um CPF com 11 dígitos e uma data válida no formato DD/MM/AAAA."
        )


def test_third_mismatch_closes_session_with_unbound_farewell(scripted_model, credit_service):
    model = scripted_model(
        responses=[
            tool_step(auth("wrong-date", date="15/05/1990")),
            AIMessage(content="Os dados não conferem."),
            tool_step(auth("wrong-cpf", cpf="98765432100")),
            AIMessage(content="Os dados não conferem."),
            tool_step(auth("third", date="16/05/1990")),
            AIMessage(content="Vou encerrar por segurança. Até logo!"),
        ]
    )
    graph = build_graph(model, credit_service)

    for count in range(1, 4):
        result = turn(graph)
        assert not result.authenticated
        assert result.conversation_ended is (count == 3)
        state = checkpoint(graph)
        assert state["auth_attempts"] == count
        assert tool_messages(state)[-1].content == "Os dados não conferem."
        assert "client_cpf" not in state
    assert result.reply == "Vou encerrar por segurança. Até logo!"
    assert model.calls[-1]["tools"] is None
    assert "despedida cordial" in model.calls[-1]["messages"][0].text
    assert turn(graph).reply == ENDED_REPLY
    graph.invoke({"messages": [HumanMessage(content="Mais uma tentativa")]}, CONFIG)
    assert len(model.calls) == 6
    assert checkpoint(graph)["auth_attempts"] == 3


def test_persistence_failure_is_controlled_and_private(
    scripted_model, credit_service, monkeypatch, caplog
):
    def fail(*args):
        raise PersistenceError(f"Erro de leitura: {CPF} {BIRTH_DATE}")

    monkeypatch.setattr(credit_service.clients, "get", fail)
    model = scripted_model(
        responses=[tool_step(auth()), AIMessage(content="Tente novamente em instantes.")]
    )
    graph = build_graph(model, credit_service)
    assert turn(graph).reply == "Tente novamente em instantes."
    state = checkpoint(graph)
    assert state.get("auth_attempts", 0) == 0
    assert not state.get("conversation_ended")
    assert tool_messages(state)[-1].content == "A autenticação está temporariamente indisponível."
    assert caplog.record_tuples == [("banco_agil.agents.tools", logging.ERROR, "PersistenceError")]
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("agent", ["credito", "cambio"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_redirect_requires_authenticated_identity(
    scripted_model, credit_service, agent, authenticated
):
    model = scripted_model(
        responses=[
            tool_step(call(f"redirecionar_para_{agent}", "early")),
            AIMessage(content="Precisamos confirmar sua identidade."),
        ]
    )
    graph = build_graph(model, credit_service)
    graph.update_state(
        CONFIG,
        {"authenticated": authenticated, "messages": [AIMessage(content="Olá!")]},
        as_node="triagem",
    )
    result = turn(graph)
    assert result.active_agent == "triagem"
    assert not result.authenticated
    state = checkpoint(graph)
    assert state["active_agent"] == "triagem"
    assert tool_messages(state)[-1].content == "Autentique o cliente primeiro."
    assert "client_cpf" not in state


@pytest.mark.parametrize(
    ("authenticated", "client_cpf", "active_agent"),
    [
        (False, "12345678901", "credito"),
        (True, None, "credito"),
        (True, "", "cambio"),
        (True, None, "entrevista"),
        (True, "12345678901", "desconhecido"),
    ],
)
def test_entry_routes_to_triage_when_gate_or_registration_fails(
    scripted_model, credit_service, authenticated, client_cpf, active_agent
):
    model = scripted_model(responses=[AIMessage(content="Olá!")])
    graph = build_graph(model, credit_service)
    graph.update_state(
        CONFIG,
        {
            "authenticated": authenticated,
            "client_cpf": client_cpf,
            "active_agent": active_agent,
            "messages": [AIMessage(content="Olá!")],
        },
        as_node="triagem",
    )
    result = turn(graph)
    assert result.active_agent == "triagem"
    assert result.authenticated is bool(authenticated and client_cpf)
    assert {tool["function"]["name"] for tool in model.calls[0]["tools"]} == TRIAGE_TOOLS
    if not result.authenticated:
        assert "solicite CPF" in model.calls[0]["messages"][0].text


@pytest.mark.parametrize("agent", ["credito", "cambio"])
def test_same_turn_handoff_persists_and_return_preserves_authentication(
    scripted_model, credit_service, agent
):
    model = scripted_model(
        responses=[
            tool_step(auth()),
            tool_step(call(f"redirecionar_para_{agent}", "handoff")),
            AIMessage(content="A consulta está indisponível."),
            tool_step(call("voltar_para_triagem", "return")),
            AIMessage(content="Ana Souza, sobre o que quer conversar?"),
        ]
    )
    graph = build_graph(model, credit_service)
    result = turn(graph, f"{CPF}, {BIRTH_DATE}")
    assert result.active_agent == checkpoint(graph)["active_agent"] == agent
    assert result.authenticated
    specialist_prompt = model.calls[2]["messages"][0].text
    assert ("crédito" if agent == "credito" else "câmbio") in specialist_prompt
    assert "Nunca anuncie transferências" in specialist_prompt
    assert "Ana Souza" in model.calls[1]["messages"][0].text

    result = turn(graph, "Quero falar de outro assunto")
    assert model.calls[3]["messages"][0].text == specialist_prompt
    assert result.active_agent == checkpoint(graph)["active_agent"] == "triagem"
    assert result.authenticated
    assert checkpoint(graph)["client_cpf"] == "12345678901"
    assert checkpoint(graph)["client_name"] == "Ana Souza"
    assert "Ana Souza" in model.calls[4]["messages"][0].text
    assert "solicite CPF" not in model.calls[4]["messages"][0].text


def test_parallel_auth_and_redirect_use_pre_auth_snapshot(scripted_model, credit_service):
    model = scripted_model(
        responses=[
            tool_step(auth(), call("redirecionar_para_credito", "early")),
            tool_step(call("redirecionar_para_credito", "retry")),
            AIMessage(content="Como posso ajudar com seu crédito?"),
        ]
    )
    graph = build_graph(model, credit_service)
    result = turn(graph)
    assert result.active_agent == "credito"
    assert result.authenticated
    messages = tool_messages(checkpoint(graph))
    assert [message.tool_call_id for message in messages] == ["auth", "early", "retry"]
    assert messages[0].content == "Cliente autenticado: Ana Souza."
    assert messages[1].content == "Autentique o cliente primeiro."
    assert "Ana Souza" in model.calls[1]["messages"][0].text


def test_parallel_duplicate_calls_keep_state_writes_and_tool_responses_consistent(
    scripted_model, credit_service
):
    model = scripted_model(
        responses=[
            tool_step(auth(), auth("duplicate")),
            tool_step(
                call("redirecionar_para_credito", "credit"),
                call("redirecionar_para_cambio", "exchange"),
            ),
            AIMessage(content="Como posso ajudar com seu crédito?"),
        ]
    )
    graph = build_graph(model, credit_service)
    result = turn(graph)
    assert result.authenticated and result.active_agent == "credito"
    messages = tool_messages(checkpoint(graph))
    assert [message.tool_call_id for message in messages] == [
        "auth",
        "duplicate",
        "credit",
        "exchange",
    ]
    assert [message.status for message in messages] == ["success", "error", "success", "error"]


def test_closure_takes_priority_over_other_calls_in_the_batch(scripted_model, credit_service):
    close = call("encerrar_atendimento", "close", motivo="Pedido do cliente")
    model = scripted_model(
        responses=[
            tool_step(auth(), close),
            AIMessage(content="Até logo!", tool_calls=[close]),
        ]
    )
    graph = build_graph(model, credit_service)
    result = turn(graph)
    assert result.conversation_ended and not result.authenticated
    assert result.reply == "Até logo!"
    assert model.calls[-1]["tools"] is None
    messages = tool_messages(checkpoint(graph))
    assert [message.tool_call_id for message in messages] == ["auth", "close"]
    assert [message.status for message in messages] == ["error", "success"]


def test_specialist_cannot_execute_triage_tool_and_errors_hide_model_input(
    scripted_model, credit_service
):
    model = scripted_model(
        responses=[
            tool_step(
                call(
                    "autenticar_cliente", "invalid", cpf={"value": CPF}, data_nascimento=BIRTH_DATE
                )
            ),
            tool_step(auth()),
            tool_step(call("redirecionar_para_credito", "credit")),
            tool_step(auth("blocked"), call(CPF, "unknown")),
            AIMessage(
                content=f"Dados: {CPF} e {BIRTH_DATE} (1990-05-14). "
                "Operação em 2025-11-21T14:30:00. Referência em 20/11/2025."
            ),
        ]
    )
    graph = build_graph(model, credit_service)
    result = turn(graph)
    assert result.authenticated and result.active_agent == "credito"
    assert result.reply == (
        "Dados: [dado omitido] e [dado omitido] ([dado omitido]). "
        "Operação em 2025-11-21T14:30:00. Referência em 20/11/2025."
    )
    errors = [message for message in tool_messages(checkpoint(graph)) if message.status == "error"]
    assert [message.tool_call_id for message in errors] == ["invalid", "blocked", "unknown"]
    for error in errors:
        assert_no_personal_data(error.model_dump_json())


@pytest.mark.parametrize("legacy_authenticated", [False, True])
def test_dates_are_masked_without_verified_birth_date(
    scripted_model, credit_service, legacy_authenticated
):
    model = scripted_model(responses=[AIMessage(content=f"Confira {CPF} e {BIRTH_DATE}.")])
    graph = build_graph(model, credit_service)
    if legacy_authenticated:
        graph.update_state(
            CONFIG,
            {
                "authenticated": True,
                "client_cpf": "12345678901",
                "messages": [AIMessage(content="Olá!")],
            },
            as_node="triagem",
        )
    result = turn(graph)
    assert result.authenticated is legacy_authenticated
    assert result.reply == "Confira [dado omitido] e [dado omitido]."

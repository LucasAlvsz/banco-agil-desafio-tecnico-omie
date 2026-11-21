import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from banco_agil.agents.graph import ENDED_REPLY, ERROR_REPLY, TurnResult, build_graph, run_turn


def closing_call():
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "encerrar_atendimento",
                "args": {"motivo": "Cliente pediu para encerrar"},
                "id": "finish-1",
                "type": "tool_call",
            }
        ],
    )


def test_history_and_session_isolation(scripted_model):
    model = scripted_model(
        responses=[AIMessage(content=text) for text in ["Olá!", "Claro!", "Bem-vindo!"]]
    )
    saver = InMemorySaver()
    graph = build_graph(model, checkpointer=saver)
    assert graph.checkpointer is saver
    assert run_turn(graph, thread_id="one", user_message="Olá").reply == "Olá!"
    assert run_turn(graph, thread_id="one", user_message="Pode ajudar?").reply == "Claro!"
    assert run_turn(graph, thread_id="two", user_message="Oi").reply == "Bem-vindo!"
    assert [[m.content for m in call["messages"][1:]] for call in model.calls] == [
        ["Olá"],
        ["Olá", "Olá!", "Pode ajudar?"],
        ["Oi"],
    ]
    assert all("Banco Ágil" in call["messages"][0].content for call in model.calls)


def test_tool_command_ends_session_and_farewell_has_no_tools(scripted_model):
    model = scripted_model(
        responses=[
            closing_call(),
            AIMessage(
                content=[
                    {"type": "reasoning", "reasoning": "texto interno"},
                    {"type": "text", "text": "Até logo!"},
                ]
            ),
        ]
    )
    graph = build_graph(model)
    result = run_turn(graph, thread_id="one", user_message="Quero encerrar")
    assert result == TurnResult(reply="Até logo!", conversation_ended=True)
    assert model.bindings == 1
    assert model.calls[0]["tools"][0]["function"]["name"] == "encerrar_atendimento"
    assert "tool_call_id" not in model.calls[0]["tools"][0]["function"]["parameters"]["properties"]
    assert model.calls[1]["tools"] is None
    tool_result = model.calls[1]["messages"][-1]
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.tool_call_id == "finish-1"
    assert "Cliente pediu para encerrar" in tool_result.content
    assert graph.get_state({"configurable": {"thread_id": "one"}}).values["conversation_ended"]
    assert run_turn(graph, thread_id="one", user_message="Mais uma pergunta").reply == ENDED_REPLY
    assert len(model.calls) == 2
    state = graph.invoke(
        {"messages": [HumanMessage(content="Entrada direta")]},
        {"configurable": {"thread_id": "one"}},
    )
    assert state["conversation_ended"] is True
    assert len(model.calls) == 2


@pytest.mark.parametrize("message", ["", " \n\t "])
def test_empty_input_does_not_call_model(scripted_model, message):
    model = scripted_model(responses=[])
    result = run_turn(build_graph(model), thread_id="one", user_message=message)
    assert result.reply == "Por favor, escreva uma mensagem."
    assert not result.conversation_ended
    assert model.calls == []


def test_provider_failure_is_safe_and_retry_preserves_history(scripted_model, caplog):
    model = scripted_model(
        responses=[RuntimeError("private-key-and-PII"), AIMessage(content="Voltei!")]
    )
    graph = build_graph(model)
    result = run_turn(graph, thread_id="one", user_message="Olá")
    assert result == TurnResult(reply=ERROR_REPLY, conversation_ended=False)
    assert "private-key-and-PII" not in result.reply
    assert caplog.record_tuples == [
        ("banco_agil.agents.graph", logging.ERROR, "Falha ao processar turno (RuntimeError)")
    ]
    assert "private-key-and-PII" not in caplog.text
    assert caplog.records[0].exc_info is None
    assert caplog.records[0].stack_info is None
    assert run_turn(graph, thread_id="one", user_message="Tentar de novo").reply == "Voltei!"
    assert [m.content for m in model.calls[1]["messages"][1:]] == ["Olá", "Tentar de novo"]


def test_failure_after_tool_keeps_session_closed(scripted_model):
    model = scripted_model(responses=[closing_call(), RuntimeError("private-provider-error")])
    graph = build_graph(model)
    assert run_turn(graph, thread_id="one", user_message="Encerrar") == TurnResult(
        reply=ERROR_REPLY, conversation_ended=True
    )
    assert run_turn(graph, thread_id="one", user_message="Oi").reply == ENDED_REPLY
    assert len(model.calls) == 2


@pytest.mark.parametrize("content", ["", [{"type": "image_url", "image_url": "ignored"}]])
def test_missing_text_has_controlled_fallback(scripted_model, content):
    model = scripted_model(responses=[AIMessage(content=content)])
    result = run_turn(build_graph(model), thread_id="one", user_message="Oi")
    assert result.reply == ERROR_REPLY
    assert not result.conversation_ended


def test_checkpoint_failure_is_controlled(scripted_model, monkeypatch):
    graph = build_graph(scripted_model(responses=[]))

    def fail(*args, **kwargs):
        raise RuntimeError("private-checkpoint-error")

    monkeypatch.setattr(graph, "get_state", fail)
    assert run_turn(graph, thread_id="one", user_message="Oi").reply == ERROR_REPLY


def test_turn_result_is_immutable():
    result = TurnResult(reply="Olá!", conversation_ended=False)
    with pytest.raises(ValidationError, match="frozen"):
        result.conversation_ended = True

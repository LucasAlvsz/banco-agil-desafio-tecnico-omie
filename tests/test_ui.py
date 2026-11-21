from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from streamlit.testing.v1 import AppTest

from banco_agil.agents.graph import ERROR_REPLY

APP = Path(__file__).resolve().parents[1] / "src/banco_agil/ui/app.py"


def test_ui_chat_closure_and_new_conversation(monkeypatch, scripted_model):
    model = scripted_model(
        responses=[
            AIMessage(content="Olá!"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "encerrar_atendimento",
                        "args": {"motivo": "A pedido do cliente"},
                        "id": "finish-ui",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Até logo!"),
            AIMessage(content="Nova conversa!"),
        ]
    )
    monkeypatch.setattr(
        "banco_agil.infrastructure.llm_factory.create_chat_model", lambda settings: model
    )
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    first_thread = app.session_state.thread_id
    app.chat_input[0].set_value("Oi").run()
    assert not app.exception
    assert app.chat_message[1].markdown[0].value == "Olá!"
    app.chat_input[0].set_value("Pode encerrar").run()
    assert not app.exception
    assert app.session_state.conversation_ended
    assert app.chat_input[0].disabled
    assert app.chat_message[-1].markdown[0].value == "Até logo!"
    assert "Atendimento encerrado" in app.info[0].value
    app.sidebar.button[0].click().run()
    assert not app.exception
    assert app.session_state.thread_id != first_thread
    assert app.session_state.history == []
    assert not app.chat_input[0].disabled
    app.chat_input[0].set_value("Recomeçar").run()
    assert app.chat_message[-1].markdown[0].value == "Nova conversa!"
    assert len(model.calls[-1]["messages"]) == 2


@pytest.mark.parametrize("failure", ["settings", "factory"])
def test_ui_initialization_errors_are_friendly(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise RuntimeError("sensitive-provider-detail")

    target = (
        "banco_agil.config.Settings"
        if failure == "settings"
        else "banco_agil.infrastructure.llm_factory.create_chat_model"
    )
    monkeypatch.setattr(target, fail)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert "Verifique a configuração" in app.error[0].value
    assert "sensitive-provider-detail" not in app.error[0].value
    assert not app.chat_input


def test_ui_provider_error_is_friendly(monkeypatch, scripted_model):
    model = scripted_model(responses=[RuntimeError("sensitive-provider-detail")])
    monkeypatch.setattr(
        "banco_agil.infrastructure.llm_factory.create_chat_model", lambda settings: model
    )
    app = AppTest.from_file(str(APP)).run()
    app.chat_input[0].set_value("Oi").run()
    assert not app.exception
    assert app.chat_message[-1].markdown[0].value == ERROR_REPLY
    assert not app.chat_input[0].disabled

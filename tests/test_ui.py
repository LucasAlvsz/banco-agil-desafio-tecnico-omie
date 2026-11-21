import pytest
from langchain_core.messages import AIMessage
from streamlit.testing.v1 import AppTest

from banco_agil.agents.graph import ERROR_REPLY

APP = "from banco_agil.ui.app import main\nmain()"


@pytest.fixture(autouse=True)
def use_temporary_csvs(monkeypatch, data_dir):
    monkeypatch.setenv("DATA_DIR", str(data_dir))


def test_ui_chat_closure_and_new_conversation(monkeypatch, scripted_model):
    model = scripted_model(
        responses=[
            AIMessage(content="Olá!"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "autenticar_cliente",
                        "args": {"cpf": "123.456.789-01", "data_nascimento": "14/05/1990"},
                        "id": "auth-ui",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "redirecionar_para_credito",
                        "args": {},
                        "id": "credit-ui",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Ana Souza, as consultas de crédito ainda estão indisponíveis."),
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
    monkeypatch.setattr("banco_agil.ui.app.create_chat_model", lambda settings: model)
    app = AppTest.from_string(APP).run()
    assert not app.exception
    first_thread = app.session_state.thread_id
    app.chat_input[0].set_value("Oi").run()
    assert not app.exception
    assert app.chat_message[1].markdown[0].value == "Olá!"
    app.chat_input[0].set_value("123.456.789-01, 14/05/1990. Quero consultar meu crédito.").run()
    assert not app.exception
    assert app.chat_message[-1].markdown[0].value == (
        "Ana Souza, as consultas de crédito ainda estão indisponíveis."
    )
    state = app.session_state.graph.get_state({"configurable": {"thread_id": first_thread}}).values
    assert state["authenticated"]
    assert state["active_agent"] == "credito"
    app.chat_input[0].set_value("Pode encerrar").run()
    assert not app.exception
    assert app.session_state.conversation_ended
    assert app.chat_input[0].disabled
    assert app.chat_message[-1].markdown[0].value == "Até logo!"
    assert model.calls[-1]["tools"] is None
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
        "banco_agil.ui.app.Settings"
        if failure == "settings"
        else "banco_agil.ui.app.create_chat_model"
    )
    monkeypatch.setattr(target, fail)
    app = AppTest.from_string(APP).run()
    assert not app.exception
    assert "Verifique a configuração" in app.error[0].value
    assert "sensitive-provider-detail" not in app.error[0].value
    assert not app.chat_input


def test_ui_provider_error_is_friendly(monkeypatch, scripted_model):
    model = scripted_model(responses=[RuntimeError("sensitive-provider-detail")])
    monkeypatch.setattr("banco_agil.ui.app.create_chat_model", lambda settings: model)
    app = AppTest.from_string(APP).run()
    app.chat_input[0].set_value("Oi").run()
    assert not app.exception
    assert app.chat_message[-1].markdown[0].value == ERROR_REPLY
    assert not app.chat_input[0].disabled

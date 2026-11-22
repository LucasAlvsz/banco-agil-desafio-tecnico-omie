import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from banco_agil.agents.graph import ERROR_REPLY
from banco_agil.config import Settings
from banco_agil.ui.client import HttpChatClient

APP = "from banco_agil.ui.app import main\nmain()"
INTERNAL_URL = "http://api.test"
PII = "123.456.789-01 14/05/1990"


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("http://api.test/", "http://api.test"),
        ("https://api.test:8443/base///", "https://api.test:8443/base"),
    ],
)
def test_api_url_is_optional_and_normalized(value, normalized):
    settings = Settings(api_base_url=value)
    assert settings.api_base_url == normalized
    assert settings.api_timeout_seconds == 60


@pytest.mark.parametrize(
    "value",
    [
        "ftp://api.test",
        "api.test",
        "https://",
        "https://api.test/a b",
        "https://api.test:xyz",
        "https://api.test:0",
        "http://bad\\host",
    ],
)
def test_api_url_rejects_invalid_values(value):
    with pytest.raises(ValidationError, match="API"):
        Settings(api_base_url=value)


@pytest.mark.parametrize("value", [0, -1, float("inf")])
def test_api_timeout_must_be_positive_and_finite(value):
    with pytest.raises(ValidationError):
        Settings(api_timeout_seconds=value)


def test_ui_http_turn_and_hitl_retry(monkeypatch, isolated_logging, caplog, chat_response):
    pending = {
        "limite_atual": 2500,
        "novo_limite_solicitado": 6000,
        "message": "Confirme o pedido.",
    }
    replies = iter(
        [
            httpx.Response(500, content=PII),
            httpx.Response(
                200,
                json=chat_response(
                    reply=pending["message"], pending_confirmation=pending, langfuse_enabled=True
                ),
            ),
            httpx.Response(500, content=PII),
            httpx.Response(
                200, json=chat_response(reply="Pedido aprovado.", conversation_ended=True)
            ),
        ]
    )
    posts = []
    health_probes = []

    def respond(request):
        assert set(request.extensions["timeout"].values()) == {3.5}
        if request.url.path == "/health":
            health_probes.append(request.url.path)
            return httpx.Response(200, json={"langfuse_enabled": False})
        assert request.url.path == "/api/v1/chat"
        posts.append(json.loads(request.content))
        return next(replies)

    def client_factory(base_url, *, timeout_seconds):
        return HttpChatClient(
            base_url, timeout_seconds=timeout_seconds, transport=httpx.MockTransport(respond)
        )

    monkeypatch.setenv("API_BASE_URL", INTERNAL_URL + "/")
    monkeypatch.setenv("API_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setattr("banco_agil.ui.client.HttpChatClient", client_factory)
    runtime_factory = Mock(side_effect=AssertionError("O runtime deve rodar na API"))
    monkeypatch.setattr("banco_agil.runtime.ConversationRuntime.from_settings", runtime_factory)

    app = AppTest.from_string(APP).run()
    initial_thread = app.session_state.thread_id
    previous_client = app.session_state.client
    assert [item.value for item in app.sidebar.caption] == [
        "Local (Langfuse desativado)",
        "API HTTP",
    ]
    app.chat_input[0].set_value("Oi").run()
    assert not app.exception
    assert app.chat_message[-1].markdown[0].value == ERROR_REPLY
    assert not app.chat_input[0].disabled
    assert app.session_state.thread_id == initial_thread
    assert app.session_state.last_telemetry is None

    app.chat_input[0].set_value("Aumentar o limite").run()
    assert not app.exception
    assert app.session_state.thread_id == "api-thread"
    assert app.chat_input[0].disabled
    assert {button.label for button in app.button} == {
        "Nova conversa",
        "Confirmar pedido",
        "Cancelar",
    }
    assert app.sidebar.caption[0].value == "Langfuse ativo"
    assert "Agente ativo: credito" in [item.value for item in app.sidebar.text]
    assert "Tokens: 0 entrada, 0 saída, 7 total" in [item.value for item in app.sidebar.text]
    telemetry = app.session_state.last_telemetry

    next(button for button in app.button if button.label == "Confirmar pedido").click().run()
    assert not app.exception
    assert app.chat_message[-1].markdown[0].value == ERROR_REPLY
    assert app.session_state.pending_confirmation.novo_limite_solicitado == 6000
    assert app.session_state.last_telemetry == telemetry
    assert app.chat_input[0].disabled

    next(button for button in app.button if button.label == "Confirmar pedido").click().run()
    assert not app.exception
    assert app.chat_message[-1].markdown[0].value == "Pedido aprovado."
    assert app.session_state.pending_confirmation is None
    assert app.session_state.conversation_ended
    assert app.sidebar.caption[0].value == "Local (Langfuse desativado)"
    assert posts == [
        {"thread_id": initial_thread, "message": "Oi"},
        {"thread_id": initial_thread, "message": "Aumentar o limite"},
        {"thread_id": "api-thread", "approve": True},
        {"thread_id": "api-thread", "approve": True},
    ]
    app.sidebar.button[0].click().run()
    assert not app.exception
    assert app.session_state.thread_id != "api-thread"
    assert app.session_state.client is previous_client
    assert health_probes == ["/health"]
    assert not app.chat_message
    assert not app.chat_input[0].disabled
    runtime_factory.assert_not_called()
    errors = [record for record in caplog.records if record.name == "banco_agil.ui.app"]
    assert [record.getMessage() for record in errors] == [
        "Falha ao enviar turno (ApiClientError)"
    ] * 2
    assert PII not in caplog.text
    assert INTERNAL_URL not in caplog.text

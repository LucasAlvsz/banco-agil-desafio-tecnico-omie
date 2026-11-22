import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from banco_agil.agents.graph import ERROR_REPLY
from banco_agil.config import Settings
from banco_agil.ui.client import ApiClientError, HttpChatClient

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


def chat_response(**changes):
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


def test_http_client_sends_contract_and_configured_timeout():
    seen = []

    def respond(request):
        assert request.url.host == "api.test"
        assert set(request.extensions["timeout"].values()) == {2.5}
        if request.method == "GET":
            assert request.url.path == "/health"
            return httpx.Response(200, json={"langfuse_enabled": True})
        assert request.method == "POST"
        assert request.url.path == "/api/v1/chat"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=chat_response())

    client = HttpChatClient(
        INTERNAL_URL, timeout_seconds=2.5, transport=httpx.MockTransport(respond)
    )
    assert client.check_health().langfuse_enabled
    assert client.send_message(thread_id="api-thread", user_message="Olá").reply == "Resposta"
    assert client.resume(thread_id="api-thread", approved=False).telemetry.total_tokens == 7
    assert seen == [
        {"thread_id": "api-thread", "message": "Olá"},
        {"thread_id": "api-thread", "approve": False},
    ]


@pytest.mark.parametrize("failure", ["timeout", 302, 500, "invalid-json", "invalid-response"])
def test_http_client_converts_transport_http_and_response_errors(failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(PII, request=request)
        if failure == "invalid-json":
            return httpx.Response(200, content=PII)
        if failure == "invalid-response":
            return httpx.Response(200, json={"reply": PII})
        return httpx.Response(failure, content=PII)

    client = HttpChatClient(INTERNAL_URL, transport=httpx.MockTransport(respond))
    assert client.check_health() is None
    with pytest.raises(ApiClientError) as caught:
        client.send_message(thread_id="api-thread", user_message=PII)
    assert PII not in str(caught.value)
    assert INTERNAL_URL not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None


def test_ui_http_turn_and_hitl_retry(monkeypatch, isolated_logging, caplog):
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

    def respond(request):
        assert set(request.extensions["timeout"].values()) == {3.5}
        if request.url.path == "/health":
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
    monkeypatch.setattr("banco_agil.ui.app.HttpChatClient", client_factory)
    runtime_factory = Mock(side_effect=AssertionError("O runtime deve rodar na API"))
    monkeypatch.setattr("banco_agil.runtime.ConversationRuntime.from_settings", runtime_factory)

    app = AppTest.from_string(APP).run()
    initial_thread = app.session_state.thread_id
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
    assert not app.chat_message
    assert not app.chat_input[0].disabled
    runtime_factory.assert_not_called()
    errors = [record for record in caplog.records if record.name == "banco_agil.ui.app"]
    assert [record.getMessage() for record in errors] == [
        "Falha na comunicação com a API (ApiClientError)"
    ] * 2
    assert PII not in caplog.text
    assert INTERNAL_URL not in caplog.text

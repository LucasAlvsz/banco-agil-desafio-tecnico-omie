import json
from unittest.mock import Mock

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage

from banco_agil import runtime as runtime_module
from banco_agil.api.schemas import ChatResponse
from banco_agil.config import Settings
from banco_agil.infrastructure.observability import LangfuseObservability
from banco_agil.runtime import ConversationRuntime
from banco_agil.ui.client import (
    ApiClientError,
    ChatClientError,
    HttpChatClient,
    LocalChatClient,
    create_chat_client,
)

INTERNAL_URL = "http://api.test"
PII = "123.456.789-01 14/05/1990"


def test_http_client_sends_contract_and_configured_timeout(chat_response):
    seen = []
    health_probes = []
    health_available = True

    def respond(request):
        assert request.url.host == "api.test"
        assert set(request.extensions["timeout"].values()) == {2.5}
        if request.method == "GET":
            assert request.url.path == "/health"
            health_probes.append(request.url.path)
            if not health_available:
                return httpx.Response(503)
            return httpx.Response(200, json={"langfuse_enabled": True})
        assert request.method == "POST"
        assert request.url.path == "/api/v1/chat"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=chat_response(langfuse_enabled="approve" in seen[-1]))

    client = HttpChatClient(
        INTERNAL_URL, timeout_seconds=2.5, transport=httpx.MockTransport(respond)
    )
    assert client.mode_label == "API HTTP"
    assert not client.langfuse_enabled
    assert not seen
    assert not health_probes
    assert client.check_health().langfuse_enabled
    assert client.langfuse_enabled
    assert client.send_message(thread_id="api-thread", user_message="Olá").reply == "Resposta"
    assert not client.langfuse_enabled
    assert client.resume(thread_id="api-thread", approved=False).telemetry.total_tokens == 7
    assert client.langfuse_enabled
    health_available = False
    assert client.check_health() is None
    assert not client.langfuse_enabled
    client.reset_conversation("api-thread")
    client.close()
    assert health_probes == ["/health", "/health"]
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
    assert isinstance(caught.value, ChatClientError)
    assert PII not in str(caught.value)
    assert INTERNAL_URL not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None


def test_local_client_returns_chat_responses_and_resets_borrowed_runtime(
    monkeypatch, scripted_model, data_dir
):
    model = scripted_model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "autenticar_cliente",
                        "args": {"cpf": "12345678901", "data_nascimento": "14/05/1990"},
                        "id": "auth",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "redirecionar_para_credito", "args": {}, "id": "credit"}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "solicitar_aumento_limite",
                        "args": {"novo_limite": 6000},
                        "id": "limit",
                    }
                ],
            ),
            AIMessage(content="Pedido cancelado."),
        ]
    )
    langfuse = Mock()
    observer = LangfuseObservability(langfuse, BaseCallbackHandler)
    monkeypatch.setattr(runtime_module, "create_langfuse_observability", lambda _: observer)
    settings = Settings(data_dir=data_dir)
    with ConversationRuntime.from_settings(settings, model=model) as runtime:
        with LocalChatClient(runtime) as client:
            assert client.mode_label == "Direto (em memória)"
            assert client.langfuse_enabled
            sent = client.send_message(thread_id="local", user_message="Quero aumentar o limite.")
            assert isinstance(sent, ChatResponse)
            assert sent.thread_id == "local"
            assert sent.langfuse_enabled
            assert sent.pending_confirmation.novo_limite_solicitado == 6000
            resumed = client.resume(thread_id="local", approved=False)
            assert isinstance(resumed, ChatResponse)
            assert resumed.thread_id == "local"
            assert resumed.langfuse_enabled
            assert resumed.reply == "Pedido cancelado."
            assert resumed.pending_confirmation is None
            assert runtime.get_state("local")
            client.reset_conversation("local")
            assert runtime.get_state("local") == {}
        langfuse.flush.assert_called_once_with()
    assert langfuse.flush.call_count == 2


def test_factory_owns_local_runtime(monkeypatch, scripted_model, data_dir):
    model = scripted_model(responses=[AIMessage(content="Olá!")])
    langfuse = Mock()
    observer = LangfuseObservability(langfuse, BaseCallbackHandler)
    monkeypatch.setattr(runtime_module, "create_chat_model", lambda _: model)
    monkeypatch.setattr(runtime_module, "create_langfuse_observability", lambda _: observer)

    client = create_chat_client(Settings(data_dir=data_dir))
    assert client.mode_label == "Direto (em memória)"
    assert client.send_message(thread_id="local", user_message="Oi").reply == "Olá!"
    client.close()
    client.close()
    langfuse.flush.assert_called_once_with()


def test_factory_probes_http_health_once_without_building_runtime(monkeypatch):
    def respond(request):
        assert request.method == "GET"
        assert request.url.path == "/health"
        return httpx.Response(200, json={"langfuse_enabled": True})

    handler = Mock(side_effect=respond)
    transport = httpx.MockTransport(handler)

    def build_http(base_url, *, timeout_seconds):
        return HttpChatClient(base_url, timeout_seconds=timeout_seconds, transport=transport)

    monkeypatch.setattr("banco_agil.ui.client.HttpChatClient", build_http)
    runtime_factory = Mock(side_effect=AssertionError("Unexpected local runtime"))
    monkeypatch.setattr(ConversationRuntime, "from_settings", runtime_factory)

    client = create_chat_client(Settings(api_base_url=INTERNAL_URL))
    assert client.mode_label == "API HTTP"
    assert client.langfuse_enabled
    handler.assert_called_once()
    runtime_factory.assert_not_called()

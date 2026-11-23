import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_litellm import ChatLiteLLM
from pydantic import ValidationError

from banco_agil.config import Settings
from banco_agil.infrastructure.llm_factory import LLM_CREDENTIAL_FIELDS, create_chat_model

FAKE_PROVIDER_CREDENTIALS = dict.fromkeys(LLM_CREDENTIAL_FIELDS, "sk-fake") | {
    "aws_region_name": "us-east-1",
}


class _CapturedHttpRequest(BaseException):
    def __init__(self, url: str, payload: dict[str, Any]) -> None:
        self.url = url
        self.payload = payload


@tool
def _sample_tool() -> str:
    """Consulta o limite atual do cliente."""
    return "R$ 5.000,00"


def _capture_outbound_request(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> tuple[str, dict[str, Any]]:
    def intercept_httpx(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        raise _CapturedHttpRequest(str(request.url), json.loads(request.content.decode("utf-8")))

    def intercept_boto(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        raw = request.body.decode("utf-8") if isinstance(request.body, bytes) else request.body
        raise _CapturedHttpRequest(str(request.url), json.loads(raw))

    monkeypatch.setattr("httpx.Client.send", intercept_httpx)
    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", intercept_boto)
    model = create_chat_model(settings)
    bound = model.bind_tools([_sample_tool])
    with pytest.raises(_CapturedHttpRequest) as captured:
        bound.invoke([SystemMessage(content="sys"), HumanMessage(content="oi")])
    return captured.value.url, captured.value.payload


def _extract_temperature(payload: dict[str, Any]) -> object:
    if "temperature" in payload:
        return payload["temperature"]
    if isinstance(payload.get("generationConfig"), dict):
        return payload["generationConfig"].get("temperature")
    if isinstance(payload.get("inferenceConfig"), dict):
        return payload["inferenceConfig"].get("temperature")
    return None


def _has_tools(payload: dict[str, Any]) -> bool:
    return bool(payload.get("tools") or payload.get("toolConfig"))


def test_defaults_and_dotenv(tmp_path):
    defaults = Settings()
    assert defaults.llm_model == "groq/openai/gpt-oss-120b"
    assert defaults.llm_temperature == 0.1
    assert defaults.llm_reasoning_effort is None
    assert defaults.llm_timeout_seconds == 45
    assert defaults.data_dir == Path("data")
    dotenv = tmp_path / ".env"
    dotenv.write_text("LLM_MODEL=gemini/gemini-2.5-flash\nDATA_DIR=seed\nUNUSED=ok\n")
    settings = Settings(_env_file=dotenv)
    assert settings.llm_model == "gemini/gemini-2.5-flash"
    assert settings.data_dir == Path("seed")


@pytest.mark.parametrize("raw_effort", [None, "", "   "])
def test_blank_reasoning_effort_normalizes_to_none(raw_effort):
    assert Settings(llm_reasoning_effort=raw_effort).llm_reasoning_effort is None


@pytest.mark.parametrize(
    ("raw_effort", "expected"),
    [
        ("none", "none"),
        (" minimal ", "minimal"),
        (" LOW ", "low"),
        ("medium", "medium"),
        ("HIGH", "high"),
    ],
)
def test_valid_reasoning_effort_is_normalized(raw_effort, expected):
    assert Settings(llm_reasoning_effort=raw_effort).llm_reasoning_effort == expected


@pytest.mark.parametrize("invalid_effort", ["turbo", "xhigh", "max"])
def test_invalid_reasoning_effort_is_rejected(invalid_effort):
    with pytest.raises(ValidationError):
        Settings(llm_reasoning_effort=invalid_effort)


@pytest.mark.parametrize("model", ["plain-model", "", "/model", "groq/", " groq/model"])
def test_model_requires_explicit_provider(model):
    with pytest.raises(ValidationError, match="provedor/modelo"):
        Settings(llm_model=model)


@pytest.mark.parametrize("model", ["groq/openai/gpt-oss-120b", "bedrock/a", "openai/b"])
def test_explicit_model_is_preserved(model):
    assert Settings(llm_model=model).llm_model == model


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_temperature", -0.1),
        ("llm_temperature", 2.1),
        ("llm_temperature", float("nan")),
        ("llm_timeout_seconds", 0),
        ("llm_timeout_seconds", -1),
        ("llm_timeout_seconds", float("inf")),
    ],
)
def test_invalid_ranges(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


@pytest.mark.parametrize("temperature", [0, 2])
def test_temperature_boundaries(temperature):
    assert Settings(llm_temperature=temperature).llm_temperature == temperature


def test_secrets_hidden_in_representation_and_validation_errors():
    sensitive_value = "test-sensitive-value"
    settings = Settings(**dict.fromkeys(LLM_CREDENTIAL_FIELDS, sensitive_value))
    assert sensitive_value not in repr(settings)
    assert sensitive_value not in str(settings)
    with pytest.raises(ValidationError) as error:
        Settings(groq_api_key={"secret": sensitive_value})
    assert sensitive_value not in str(error.value)


def test_factory_returns_configured_chat_model():
    model = create_chat_model(Settings(llm_temperature=0.4, llm_timeout_seconds=12))
    assert isinstance(model, BaseChatModel)
    assert isinstance(model, ChatLiteLLM)
    assert model.model == "groq/openai/gpt-oss-120b"
    assert model.temperature == 0.4
    assert model.request_timeout == 12
    assert model.max_retries == 2
    assert model.model_kwargs == {"drop_params": True}


def test_factory_forwards_reasoning_effort_when_configured():
    model = create_chat_model(Settings(llm_model="openai/gpt-6-luna", llm_reasoning_effort="none"))
    assert isinstance(model, ChatLiteLLM)
    assert model.model == "openai/gpt-6-luna"
    assert model.model_kwargs == {"drop_params": True, "reasoning_effort": "none"}


@pytest.mark.parametrize(
    ("model_id", "expected_endpoint", "expected_temperature"),
    [
        ("groq/openai/gpt-oss-120b", "/chat/completions", 0.1),
        ("gemini/gemini-2.5-flash", "generateContent", 0.1),
        ("openai/gpt-4o-mini", "/chat/completions", 0.1),
        ("openai/gpt-6-luna", "/responses", None),
        ("openai/gpt-5-mini", "/chat/completions", None),
        ("openai/o4-mini", "/chat/completions", None),
        ("openrouter/openai/gpt-4o-mini", "/chat/completions", 0.1),
        ("bedrock/anthropic.claude-3-haiku-20240307-v1:0", "/converse", 0.1),
    ],
)
def test_documented_providers_and_reasoning_models_build_valid_requests(
    monkeypatch, model_id, expected_endpoint, expected_temperature
):
    url, payload = _capture_outbound_request(
        monkeypatch,
        Settings(llm_model=model_id, llm_temperature=0.1, **FAKE_PROVIDER_CREDENTIALS),
    )
    assert expected_endpoint in url
    assert "drop_params" not in payload
    assert _has_tools(payload)
    assert _extract_temperature(payload) == expected_temperature


@pytest.mark.parametrize(
    "model_id",
    [
        "groq/openai/gpt-oss-120b",
        "gemini/gemini-2.5-flash",
        "openai/gpt-6-luna",
        "openrouter/openai/gpt-4o-mini",
        "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
    ],
)
@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high"])
def test_all_valid_reasoning_efforts_work_across_documented_providers(
    monkeypatch, model_id, effort
):
    url, payload = _capture_outbound_request(
        monkeypatch,
        Settings(
            llm_model=model_id,
            llm_temperature=0.1,
            llm_reasoning_effort=effort,
            **FAKE_PROVIDER_CREDENTIALS,
        ),
    )
    assert "drop_params" not in payload
    assert _has_tools(payload)
    if model_id == "openai/gpt-6-luna" and effort == "none":
        assert "/chat/completions" in url
        assert payload["temperature"] == 0.1
        assert payload["reasoning_effort"] == "none"


@pytest.mark.parametrize("field", LLM_CREDENTIAL_FIELDS)
def test_factory_exports_secrets_without_overriding_environment(monkeypatch, field):
    variable = field.upper()
    settings = Settings(**{field: "from-settings"})
    create_chat_model(settings)
    assert os.environ[variable] == "from-settings"
    monkeypatch.setenv(variable, "explicit-environment")
    create_chat_model(settings)
    assert os.environ[variable] == "explicit-environment"
    monkeypatch.setenv(variable, "")
    create_chat_model(settings)
    assert os.environ[variable] == "from-settings"


def test_empty_credentials_are_not_exported():
    create_chat_model(Settings(**dict.fromkeys(LLM_CREDENTIAL_FIELDS, "")))
    assert all(field.upper() not in os.environ for field in LLM_CREDENTIAL_FIELDS)


@pytest.mark.parametrize("url", [None, " "])
def test_mcp_exchange_url_is_required_even_without_settings_validation(url):
    settings = Settings.model_construct(mcp_exchange_url=url)
    with pytest.raises(
        ValueError, match=r"^Configure MCP_EXCHANGE_URL para usar streamable_http\.$"
    ):
        settings.resolve_mcp_exchange_url()


def test_mcp_exchange_url_resolves_validated_url():
    settings = Settings(
        mcp_exchange_transport="streamable_http", mcp_exchange_url="http://localhost:8001/mcp/"
    )
    assert settings.resolve_mcp_exchange_url() == "http://localhost:8001/mcp"


def test_network_is_blocked():
    with socket.socket() as connection, pytest.raises(AssertionError, match="Rede proibida"):
        connection.connect(("127.0.0.1", 80))
    with pytest.raises(AssertionError, match="Rede proibida"):
        socket.getaddrinfo("example.com", 443)

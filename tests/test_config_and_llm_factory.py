import os
import socket
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_litellm import ChatLiteLLM
from pydantic import ValidationError

from banco_agil.config import Settings
from banco_agil.infrastructure.llm_factory import create_chat_model

SECRET_FIELDS = (
    "groq_api_key",
    "gemini_api_key",
    "openai_api_key",
    "openrouter_api_key",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_region_name",
)


def test_defaults_and_dotenv(tmp_path):
    defaults = Settings()
    assert defaults.llm_model == "groq/openai/gpt-oss-120b"
    assert defaults.llm_temperature == 0.1
    assert defaults.llm_timeout_seconds == 45
    assert defaults.data_dir == Path("data")
    dotenv = tmp_path / ".env"
    dotenv.write_text("LLM_MODEL=gemini/gemini-2.5-flash\nDATA_DIR=seed\nUNUSED=ok\n")
    settings = Settings(_env_file=dotenv)
    assert settings.llm_model == "gemini/gemini-2.5-flash"
    assert settings.data_dir == Path("seed")


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
    settings = Settings(**dict.fromkeys(SECRET_FIELDS, sensitive_value))
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


@pytest.mark.parametrize("field", SECRET_FIELDS)
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
    create_chat_model(Settings(**dict.fromkeys(SECRET_FIELDS, "")))
    assert all(field.upper() not in os.environ for field in SECRET_FIELDS)


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

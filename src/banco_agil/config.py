from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True, allow_inf_nan=False
    )

    llm_model: str = "groq/openai/gpt-oss-120b"
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_timeout_seconds: float = Field(default=45.0, gt=0.0)
    exchange_timeout_seconds: float = Field(default=8.0, gt=0.0)
    api_base_url: str | None = None
    api_timeout_seconds: float = Field(default=60.0, gt=0.0)
    data_dir: Path = Path("data")
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_region_name: SecretStr | None = None

    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str | None = None
    langfuse_host: str | None = Field(default=None, exclude=True, repr=False)

    @field_validator("llm_model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        provider, separator, model = value.partition("/")
        if not separator or not provider.strip() or not model.strip() or value != value.strip():
            raise ValueError("Use o formato explícito provedor/modelo")
        return value

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        error = "Informe uma URL HTTP ou HTTPS válida para a API"
        if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
            raise ValueError(error)
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError:
            raise ValueError(error) from None
        if url.scheme not in ("http", "https") or not url.hostname or port == 0:
            raise ValueError(error)
        return value.rstrip("/")

from pathlib import Path

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

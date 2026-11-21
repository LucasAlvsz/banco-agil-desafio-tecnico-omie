import os

from langchain_core.language_models import BaseChatModel

from banco_agil.config import Settings


def create_chat_model(settings: Settings) -> BaseChatModel:
    from langchain_litellm import ChatLiteLLM

    for field in (
        "groq_api_key",
        "gemini_api_key",
        "openai_api_key",
        "openrouter_api_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_region_name",
    ):
        secret = getattr(settings, field)
        if secret and secret.get_secret_value() and not os.environ.get(field.upper()):
            os.environ[field.upper()] = secret.get_secret_value()

    return ChatLiteLLM(
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        request_timeout=settings.llm_timeout_seconds,
        max_retries=2,
    )

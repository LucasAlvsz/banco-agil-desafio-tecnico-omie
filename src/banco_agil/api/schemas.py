from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from banco_agil.agents.graph import TurnResult
from banco_agil.infrastructure.observability import TurnTelemetry


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: str = "banco-agil"
    langfuse_enabled: bool


class ChatRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str | None = Field(default=None, min_length=1, max_length=128)
    message: str | None = None
    approve: StrictBool | None = None

    @field_validator("thread_id")
    @classmethod
    def validate_thread_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Informe um identificador de conversa válido.")
        return value

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if (self.message is None) == (self.approve is None):
            raise ValueError("Informe exatamente um dos campos message ou approve.")
        return self


class ChatResponse(TurnResult):
    thread_id: str
    telemetry: TurnTelemetry = Field()
    langfuse_enabled: bool

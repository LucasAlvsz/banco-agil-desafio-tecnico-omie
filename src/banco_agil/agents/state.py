from typing import Annotated, Any, Literal, NotRequired, Self, TypedDict, get_args

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

AgentName = Literal["triagem", "credito", "entrevista", "cambio"]
AGENT_NODES: tuple[AgentName, ...] = get_args(AgentName)
CONFIRM_LIMIT_INCREASE_ACTION = "confirm_limit_increase"
DEFAULT_LIMIT_INCREASE_CONFIRMATION_MESSAGE = "Você confirma o pedido de aumento de limite?"


class LimitIncreaseConfirmation(BaseModel):
    model_config = ConfigDict(
        frozen=True, allow_inf_nan=False, json_schema_serialization_defaults_required=True
    )

    limite_atual: float
    novo_limite_solicitado: float
    # Keep the OpenAPI response field required while supplying the message internally.
    message: str = Field(default_factory=lambda: DEFAULT_LIMIT_INCREASE_CONFIRMATION_MESSAGE)

    def to_interrupt_payload(self) -> dict[str, Any]:
        return {"action": CONFIRM_LIMIT_INCREASE_ACTION, **self.model_dump()}

    @classmethod
    def from_interrupt_value(cls, value: Any) -> Self | None:
        if not isinstance(value, dict) or value.get("action") != CONFIRM_LIMIT_INCREASE_ACTION:
            return None
        return cls.model_validate(value)


class LimitIncreaseDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approved: StrictBool

    def to_resume_payload(self) -> dict[str, bool]:
        return {"approved": self.approved}

    @classmethod
    def is_approved(cls, value: Any) -> bool:
        try:
            return cls.model_validate(value).approved
        except ValidationError:
            return False


class ConversationState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    active_agent: NotRequired[AgentName]
    authenticated: NotRequired[bool]
    client_cpf: NotRequired[str | None]
    client_name: NotRequired[str | None]
    client_birth_date: NotRequired[str]
    auth_attempts: NotRequired[int]
    conversation_ended: NotRequired[bool]
    pending_requested_limit: NotRequired[float | None]


def has_authenticated_client(state: ConversationState) -> bool:
    return bool(state.get("authenticated") and state.get("client_cpf"))

from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

AgentName = Literal["triagem", "credito", "entrevista", "cambio"]


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

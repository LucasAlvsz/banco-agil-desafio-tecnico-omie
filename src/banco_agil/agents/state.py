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
    auth_attempts: NotRequired[int]
    conversation_ended: NotRequired[bool]

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ConversationState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    conversation_ended: bool

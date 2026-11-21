from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command


@tool
def encerrar_atendimento(
    motivo: str, tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command[Any]:
    """Encerra o atendimento quando o cliente pede para finalizar a conversa."""
    return Command(
        update={
            "conversation_ended": True,
            "messages": [
                ToolMessage(content=f"Atendimento encerrado: {motivo}", tool_call_id=tool_call_id)
            ],
        }
    )

from contextlib import suppress
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, ConfigDict

from banco_agil.agents.state import ConversationState
from banco_agil.agents.tools import encerrar_atendimento

SYSTEM_PROMPT = (
    "Você é o assistente do Banco Ágil. Seja cordial, objetivo e fale em português, "
    "mantendo uma única persona. Quando o cliente pedir para encerrar a conversa, "
    "acione encerrar_atendimento. Não invente informações bancárias nem execute "
    "operações indisponíveis."
)
ENDED_REPLY = "Este atendimento já foi encerrado. Inicie uma nova conversa para continuar."
ERROR_REPLY = "Não foi possível responder agora. Por favor, tente novamente em instantes."

type ConversationGraph = CompiledStateGraph[
    ConversationState, None, ConversationState, ConversationState
]


class TurnResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    reply: str
    conversation_ended: bool


def build_graph(
    model: BaseChatModel, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> ConversationGraph:
    def entry(state: ConversationState) -> Literal["assistant", "__end__"]:
        return "__end__" if state.get("conversation_ended", False) else "assistant"

    def assistant(state: ConversationState) -> dict[str, list[AIMessage]]:
        ended = state.get("conversation_ended", False)
        prompt = SYSTEM_PROMPT
        if ended:
            prompt += " O atendimento foi encerrado. Responda apenas com uma despedida cordial."
        chat = model if ended else model.bind_tools([encerrar_atendimento])
        response = chat.invoke([SystemMessage(content=prompt), *state["messages"]])
        return {"messages": [response]}

    def route_tools(state: ConversationState) -> Literal["tools", "__end__"]:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "__end__"

    graph = StateGraph(ConversationState)
    graph.add_node("assistant", assistant)
    graph.add_node("tools", ToolNode([encerrar_atendimento]))
    graph.add_conditional_edges(START, entry)
    graph.add_conditional_edges("assistant", route_tools)
    graph.add_edge("tools", "assistant")
    return graph.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())


def run_turn(graph: ConversationGraph, *, thread_id: str, user_message: str) -> TurnResult:
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    ended = False
    try:
        ended = bool(graph.get_state(config).values.get("conversation_ended", False))
        if ended:
            return TurnResult(reply=ENDED_REPLY, conversation_ended=True)
        if not user_message.strip():
            return TurnResult(reply="Por favor, escreva uma mensagem.", conversation_ended=False)
        state = graph.invoke(
            {"messages": [HumanMessage(content=user_message)], "conversation_ended": False}, config
        )
        ended = bool(state.get("conversation_ended", False))
        last = state["messages"][-1]
        reply = last.text.strip() if isinstance(last, AIMessage) else ""
        return TurnResult(reply=reply or ERROR_REPLY, conversation_ended=ended)
    except Exception:
        # A ferramenta pode ter encerrado a sessão antes de a despedida falhar.
        with suppress(Exception):
            ended = bool(graph.get_state(config).values.get("conversation_ended", False))
        return TurnResult(reply=ERROR_REPLY, conversation_ended=ended)

import logging
import re
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import Any, Literal, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolInvocationError
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, ConfigDict

from banco_agil.agents.prompts import (
    build_credit_prompt,
    build_exchange_placeholder_prompt,
    build_interview_placeholder_prompt,
    build_triage_prompt,
)
from banco_agil.agents.state import AgentName, ConversationState, has_authenticated_client
from banco_agil.agents.tools import (
    build_credit_tools,
    build_triage_tools,
    encerrar_atendimento,
    voltar_para_triagem,
)
from banco_agil.domain.errors import DomainValidationError
from banco_agil.domain.rules import normalize_birth_date
from banco_agil.domain.services import CreditService

logger = logging.getLogger(__name__)

ENDED_REPLY = "Este atendimento já foi encerrado. Inicie uma nova conversa para continuar."
ERROR_REPLY = "Não foi possível responder agora. Por favor, tente novamente em instantes."
PENDING_REPLY = "Confirme ou cancele o pedido de aumento para continuar."
NO_CONFIRMATION_REPLY = "Não há pedido de aumento aguardando confirmação."
CPF_IN_REPLY = re.compile(r"(?<!\d)[0-9](?:[.\-\s]*[0-9]){10}(?!\d)")
DATE_IN_REPLY = re.compile(
    r"(?<!\d)(?:[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}|"
    r"[0-9]{1,2}([/.-])[0-9]{1,2}\1[0-9]{4})(?!\d)"
)
AGENT_NODES = ("triagem", "credito", "entrevista", "cambio")
HANDOFF_TOOLS = {
    "redirecionar_para_credito",
    "redirecionar_para_entrevista",
    "redirecionar_para_cambio",
    "voltar_para_triagem",
}

type ConversationGraph = CompiledStateGraph[
    ConversationState, None, ConversationState, ConversationState
]


class LimitIncreaseConfirmation(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    limite_atual: float
    novo_limite_solicitado: float
    message: str


class TurnResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    reply: str
    conversation_ended: bool
    active_agent: AgentName
    authenticated: bool
    pending_confirmation: LimitIncreaseConfirmation | None = None


def _active_agent(state: ConversationState) -> AgentName:
    active = state.get("active_agent", "triagem")
    return active if has_authenticated_client(state) and active in AGENT_NODES else "triagem"


def _redact_reply(reply: str, state: ConversationState) -> str:
    reply = CPF_IN_REPLY.sub("[dado omitido]", reply)
    birth_date = state.get("client_birth_date")
    # Older checkpoints may lack a verified date to compare against.
    if not has_authenticated_client(state) or not birth_date:
        return DATE_IN_REPLY.sub("[dado omitido]", reply)

    def redact_birth_date(match: re.Match[str]) -> str:
        candidate = match.group()
        with suppress(DomainValidationError):
            if normalize_birth_date(candidate) == birth_date:
                return "[dado omitido]"
        return candidate

    return DATE_IN_REPLY.sub(redact_birth_date, reply)


def _route_agent(
    state: ConversationState, *, at_entry: bool = False
) -> AgentName | Literal["__end__"]:
    if at_entry and state.get("conversation_ended"):
        return "__end__"
    return _active_agent(state)


def _guard_tool_call(
    request: ToolCallRequest, execute: Callable[[ToolCallRequest], ToolMessage | Command[Any]]
) -> ToolMessage | Command[Any]:
    call_id = request.tool_call["id"] or ""
    if request.tool is None:
        return ToolMessage("Ferramenta indisponível.", tool_call_id=call_id, status="error")

    # Parallel tools share a snapshot. Allow one write per operation, with closure taking priority.
    last: AIMessage = request.state["messages"][-1]
    name = request.tool.name
    group = HANDOFF_TOOLS if name in HANDOFF_TOOLS else {name}
    closing = next(
        (call for call in last.tool_calls if call["name"] == encerrar_atendimento.name), None
    )
    first = (
        closing
        if closing is not None
        else next(call for call in last.tool_calls if call["name"] in group)
    )
    if first["id"] != call_id:
        return ToolMessage("Ação não executada neste lote.", tool_call_id=call_id, status="error")
    return execute(request)


def _invalid_tool_arguments(_: ToolInvocationError) -> str:
    return "Revise os dados informados para esta operação."


def _add_agent_nodes(
    graph: StateGraph[ConversationState, None, ConversationState, ConversationState],
    agent: AgentName,
    model: BaseChatModel,
    build_prompt: Callable[[ConversationState], str],
    tools: list[BaseTool],
) -> None:
    def respond(state: ConversationState) -> dict[str, list[AIMessage]]:
        ended = state.get("conversation_ended", False)
        prompt = build_prompt(state)
        if ended:
            prompt += " O atendimento foi encerrado. Responda apenas com uma despedida cordial."
        chat = model if ended else model.bind_tools(tools)
        response = chat.invoke([SystemMessage(content=prompt), *state["messages"]])
        return {"messages": [response]}

    def route_tools(state: ConversationState) -> Literal["tools", "__end__"]:
        return "__end__" if state.get("conversation_ended") else tools_condition(state["messages"])

    tool_node = f"{agent}_tools"
    graph.add_node(agent, respond)
    graph.add_node(
        tool_node,
        ToolNode(
            tools, wrap_tool_call=_guard_tool_call, handle_tool_errors=_invalid_tool_arguments
        ),
    )
    graph.add_conditional_edges(agent, route_tools, {"tools": tool_node, END: END})
    graph.add_conditional_edges(tool_node, _route_agent, list(AGENT_NODES))


def build_graph(
    model: BaseChatModel,
    credit_service: CreditService,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> ConversationGraph:
    graph = StateGraph(ConversationState)
    triage_tools = [*build_triage_tools(credit_service), encerrar_atendimento]
    specialist_tools = [voltar_para_triagem, encerrar_atendimento]
    credit_tools = [*build_credit_tools(credit_service), *specialist_tools]
    _add_agent_nodes(graph, "triagem", model, build_triage_prompt, triage_tools)
    _add_agent_nodes(graph, "credito", model, build_credit_prompt, credit_tools)
    _add_agent_nodes(
        graph, "entrevista", model, build_interview_placeholder_prompt, specialist_tools
    )
    _add_agent_nodes(graph, "cambio", model, build_exchange_placeholder_prompt, specialist_tools)
    graph.add_conditional_edges(START, partial(_route_agent, at_entry=True), [*AGENT_NODES, END])
    return graph.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())


def run_turn(graph: ConversationGraph, *, thread_id: str, user_message: str) -> TurnResult:
    turn_input: ConversationState | None = None
    if user_message.strip():
        turn_input = {"messages": [HumanMessage(content=user_message)]}
    return _execute_turn(graph, thread_id, turn_input)


def resume_turn(graph: ConversationGraph, *, thread_id: str, approved: bool) -> TurnResult:
    return _execute_turn(graph, thread_id, Command(resume={"approved": approved}))


def _pending_confirmation(snapshot: StateSnapshot) -> LimitIncreaseConfirmation | None:
    for item in snapshot.interrupts:
        value = item.value
        if isinstance(value, dict) and value.get("action") == "confirm_limit_increase":
            return LimitIncreaseConfirmation.model_validate(value)
    return None


def _execute_turn(
    graph: ConversationGraph, thread_id: str, turn_input: ConversationState | Command[Any] | None
) -> TurnResult:
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    state: ConversationState = {"messages": []}
    confirmation = None
    try:
        snapshot = graph.get_state(config)
        state = cast(ConversationState, snapshot.values)
        confirmation = _pending_confirmation(snapshot)
        resuming = isinstance(turn_input, Command)
        if state.get("conversation_ended"):
            reply = ENDED_REPLY
        elif not resuming and snapshot.interrupts:
            reply = PENDING_REPLY
        elif resuming and confirmation is None:
            reply = NO_CONFIRMATION_REPLY
        elif turn_input is None:
            reply = "Por favor, escreva uma mensagem."
        else:
            graph.invoke(turn_input, config)
            snapshot = graph.get_state(config)
            state = cast(ConversationState, snapshot.values)
            confirmation = _pending_confirmation(snapshot)
            if confirmation is not None:
                reply = confirmation.message
            else:
                last = state["messages"][-1]
                reply = last.text.strip() if isinstance(last, AIMessage) else ""
    except Exception as exc:
        logger.error("Falha ao processar turno (%s)", type(exc).__name__)
        # A ferramenta pode ter encerrado a sessão antes de a despedida falhar.
        with suppress(Exception):
            snapshot = graph.get_state(config)
            state = cast(ConversationState, snapshot.values)
            confirmation = _pending_confirmation(snapshot)
        reply = ERROR_REPLY

    return TurnResult(
        reply=_redact_reply(reply, state) if reply else ERROR_REPLY,
        conversation_ended=state.get("conversation_ended", False),
        active_agent=_active_agent(state),
        authenticated=has_authenticated_client(state),
        pending_confirmation=confirmation if not state.get("conversation_ended") else None,
    )

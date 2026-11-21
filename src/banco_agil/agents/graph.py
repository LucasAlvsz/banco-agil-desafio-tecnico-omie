import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from functools import partial
from time import perf_counter
from typing import Any, Literal, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolInvocationError
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, ConfigDict, Field

from banco_agil.agents.guardrails import RECURSION_LIMIT_REPLY, evaluate_user_input, sanitize_reply
from banco_agil.agents.prompts import (
    build_credit_prompt,
    build_exchange_prompt,
    build_interview_prompt,
    build_triage_prompt,
)
from banco_agil.agents.state import AgentName, ConversationState, has_authenticated_client
from banco_agil.agents.tools import (
    build_credit_tools,
    build_exchange_tools,
    build_interview_tools,
    build_triage_tools,
    encerrar_atendimento,
    voltar_para_triagem,
)
from banco_agil.domain.repositories import ExchangeRateProvider
from banco_agil.domain.services import CreditService
from banco_agil.infrastructure.exchange_client import ExchangeRateClient
from banco_agil.infrastructure.observability import TurnTelemetry, TurnTelemetryCallback

logger = logging.getLogger(__name__)

ENDED_REPLY = "Este atendimento já foi encerrado. Inicie uma nova conversa para continuar."
ERROR_REPLY = "Não foi possível responder agora. Por favor, tente novamente em instantes."
PENDING_REPLY = "Confirme ou cancele o pedido de aumento para continuar."
NO_CONFIRMATION_REPLY = "Não há pedido de aumento aguardando confirmação."
DEFAULT_RECURSION_LIMIT = 16
AGENT_NODES = ("triagem", "credito", "entrevista", "cambio")
HANDOFF_TOOLS = {
    "calcular_e_atualizar_score",
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
    telemetry: TurnTelemetry = Field(default_factory=lambda: TurnTelemetry(latency_ms=0.0))


def _active_agent(state: ConversationState) -> AgentName:
    active = state.get("active_agent", "triagem")
    return active if has_authenticated_client(state) and active in AGENT_NODES else "triagem"


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
    exchange_provider: ExchangeRateProvider | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> ConversationGraph:
    graph = StateGraph(ConversationState)
    triage_tools = [*build_triage_tools(credit_service), encerrar_atendimento]
    specialist_tools = [voltar_para_triagem, encerrar_atendimento]
    credit_tools = [*build_credit_tools(credit_service), *specialist_tools]
    interview_tools = [*build_interview_tools(credit_service), *specialist_tools]
    provider = exchange_provider if exchange_provider is not None else ExchangeRateClient()
    exchange_tools = [*build_exchange_tools(provider), *specialist_tools]
    _add_agent_nodes(graph, "triagem", model, build_triage_prompt, triage_tools)
    _add_agent_nodes(graph, "credito", model, build_credit_prompt, credit_tools)
    _add_agent_nodes(graph, "entrevista", model, build_interview_prompt, interview_tools)
    _add_agent_nodes(graph, "cambio", model, build_exchange_prompt, exchange_tools)
    graph.add_conditional_edges(START, partial(_route_agent, at_entry=True), [*AGENT_NODES, END])
    return graph.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())


def run_turn(
    graph: ConversationGraph,
    *,
    thread_id: str,
    user_message: str,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    callbacks: Sequence[BaseCallbackHandler] = (),
    model_name: str | None = None,
) -> TurnResult:
    return _execute_turn(graph, thread_id, user_message, recursion_limit, callbacks, model_name)


def resume_turn(
    graph: ConversationGraph,
    *,
    thread_id: str,
    approved: bool,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    callbacks: Sequence[BaseCallbackHandler] = (),
    model_name: str | None = None,
) -> TurnResult:
    return _execute_turn(
        graph,
        thread_id,
        Command(resume={"approved": approved}),
        recursion_limit,
        callbacks,
        model_name,
    )


def _pending_confirmation(snapshot: StateSnapshot) -> LimitIncreaseConfirmation | None:
    for item in snapshot.interrupts:
        value = item.value
        if isinstance(value, dict) and value.get("action") == "confirm_limit_increase":
            return LimitIncreaseConfirmation.model_validate(value)
    return None


def _execute_turn(
    graph: ConversationGraph,
    thread_id: str,
    turn_input: str | Command[Any],
    recursion_limit: int,
    callbacks: Sequence[BaseCallbackHandler],
    model_name: str | None,
) -> TurnResult:
    started = perf_counter()
    telemetry = TurnTelemetryCallback(default_model_name=model_name)
    guardrail_blocked = False
    if recursion_limit < 2:
        raise ValueError("recursion_limit deve ser maior ou igual a 2")
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    state: ConversationState = {"messages": []}
    confirmation = None
    reply: str | None
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
        elif (
            isinstance(turn_input, str)
            and not (decision := evaluate_user_input(turn_input)).allowed
        ):
            if decision.reason != "empty":
                logger.warning("Entrada bloqueada por guardrail (%s)", decision.reason)
            guardrail_blocked = True
            reply = decision.reply
        else:
            invocation: ConversationState | Command[Any] = (
                {"messages": [HumanMessage(content=turn_input.replace("\x00", "").strip())]}
                if isinstance(turn_input, str)
                else turn_input
            )
            graph.invoke(
                invocation,
                config
                | {
                    "callbacks": [telemetry, *callbacks],
                    "metadata": {"langfuse_session_id": thread_id, "langfuse_tags": ["banco-agil"]},
                },
            )
            snapshot = graph.get_state(config)
            state = cast(ConversationState, snapshot.values)
            confirmation = _pending_confirmation(snapshot)
            if confirmation is not None:
                reply = confirmation.message
            else:
                last = state["messages"][-1]
                reply = last.text.strip() if isinstance(last, AIMessage) else ""
    except Exception as exc:
        if isinstance(exc, GraphRecursionError):
            logger.warning("Limite de passos atingido no turno (%s)", type(exc).__name__)
            reply = RECURSION_LIMIT_REPLY
        else:
            logger.error("Falha ao processar turno (%s)", type(exc).__name__)
            reply = ERROR_REPLY
        # A ferramenta pode ter encerrado a sessão antes de a despedida falhar.
        with suppress(Exception):
            snapshot = graph.get_state(config)
            state = cast(ConversationState, snapshot.values)
            confirmation = _pending_confirmation(snapshot)
            if (
                isinstance(exc, GraphRecursionError)
                and not snapshot.interrupts
                and (messages := state.get("messages"))
                and isinstance(last := messages[-1], AIMessage)
                and last.tool_calls
                and last.id
            ):
                # An odd budget or HITL resume can stop before the ToolNode runs.
                graph.update_state(
                    config, {"messages": [RemoveMessage(id=last.id)]}, as_node=_active_agent(state)
                )

    reply = sanitize_reply(reply, state) if reply else ERROR_REPLY
    active_agent = _active_agent(state)
    if not telemetry.agent_route:
        telemetry.agent_route.append(active_agent)
    return TurnResult(
        reply=reply,
        conversation_ended=state.get("conversation_ended", False),
        active_agent=active_agent,
        authenticated=has_authenticated_client(state),
        pending_confirmation=confirmation if not state.get("conversation_ended") else None,
        telemetry=telemetry.build(
            (perf_counter() - started) * 1000, guardrail_blocked=guardrail_blocked
        ),
    )

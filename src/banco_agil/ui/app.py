import logging
from decimal import Decimal
from uuid import uuid4

import streamlit as st

from banco_agil.agents.graph import ERROR_REPLY, LimitIncreaseConfirmation
from banco_agil.agents.state import AgentName
from banco_agil.api.schemas import ChatResponse
from banco_agil.infrastructure.observability import TurnTelemetry
from banco_agil.infrastructure.privacy import configure_logging
from banco_agil.ui.client import ChatClient, ChatClientError, create_chat_client

logger = logging.getLogger(__name__)


def _format_limit(amount: float) -> str:
    whole, _, fraction = format(Decimal(str(amount)), ",f").partition(".")
    return f"R$ {whole.replace(',', '.')},{fraction.ljust(2, '0')}"


def _record_reply(result: ChatResponse) -> None:
    st.session_state.thread_id = result.thread_id
    st.session_state.history.append({"role": "assistant", "content": result.reply})
    st.session_state.pending_confirmation = result.pending_confirmation
    st.session_state.conversation_ended = result.conversation_ended
    st.session_state.last_telemetry = result.telemetry
    st.session_state.active_agent = result.active_agent
    st.rerun()


def _submit_turn(*, user_message: str | None = None, approved: bool | None = None) -> None:
    thread_id: str = st.session_state.thread_id
    client: ChatClient = st.session_state.client
    try:
        with st.spinner("Aguarde..."):
            if user_message is not None:
                result = client.send_message(thread_id=thread_id, user_message=user_message)
            else:
                assert approved is not None
                result = client.resume(thread_id=thread_id, approved=approved)
    except ChatClientError as exc:
        logger.error("Falha ao enviar turno (%s)", type(exc).__name__)
        st.session_state.history.append({"role": "assistant", "content": ERROR_REPLY})
        st.rerun()
        return

    _record_reply(result)


def _show_telemetry(
    telemetry: TurnTelemetry | None,
    active_agent: AgentName,
    *,
    langfuse_enabled: bool,
    mode_label: str,
) -> None:
    with st.sidebar:
        st.subheader("Telemetria do turno")
        st.caption("Langfuse ativo" if langfuse_enabled else "Local (Langfuse desativado)")
        st.caption(mode_label)
        st.text(f"Agente ativo: {active_agent}")
        if telemetry is not None:
            st.text(f"Rota: {' → '.join(telemetry.agent_route)}")
            st.text(f"Latência: {telemetry.latency_ms:.1f} ms")
            st.text(
                f"Tokens: {telemetry.input_tokens} entrada, "
                f"{telemetry.output_tokens} saída, {telemetry.total_tokens} total"
            )
            cost = telemetry.estimated_cost_usd
            st.text(f"Custo estimado: {'N/D' if cost is None else f'US$ {cost:.6f}'}")


def main() -> None:
    configure_logging()
    st.set_page_config(page_title="Banco Ágil", page_icon="🏦")
    st.title("Banco Ágil")

    new_conversation = st.sidebar.button("Nova conversa")
    if new_conversation and "client" in st.session_state and "thread_id" in st.session_state:
        previous_client: ChatClient = st.session_state.client
        previous_client.reset_conversation(st.session_state.thread_id)
    if new_conversation or "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid4())
        st.session_state.history = []
        st.session_state.conversation_ended = False
        st.session_state.pending_confirmation = None
        st.session_state.last_telemetry = None
        st.session_state.active_agent = "triagem"

    try:
        if "client" not in st.session_state:
            st.session_state.client = create_chat_client()
    except Exception as exc:
        logger.error("Falha na inicialização do atendimento (%s)", type(exc).__name__)
        st.error(
            "Não foi possível iniciar o atendimento. Verifique a configuração do provedor LLM."
        )
        st.stop()

    client: ChatClient = st.session_state.client
    _show_telemetry(
        st.session_state.last_telemetry,
        st.session_state.active_agent,
        langfuse_enabled=client.langfuse_enabled,
        mode_label=client.mode_label,
    )

    for message in st.session_state.history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if st.session_state.conversation_ended:
        st.info("Atendimento encerrado. Clique em Nova conversa para iniciar outro atendimento.")

    pending: LimitIncreaseConfirmation | None = st.session_state.get("pending_confirmation")
    if pending is not None:
        current = _format_limit(pending.limite_atual)
        requested = _format_limit(pending.novo_limite_solicitado)
        st.warning(f"Limite atual **{current}** → Novo limite **{requested}**")
        confirm = st.button("Confirmar pedido", type="primary")
        cancel = st.button("Cancelar")
        if confirm or cancel:
            _submit_turn(approved=confirm and not cancel)

    disabled = st.session_state.conversation_ended or pending is not None
    if (prompt := st.chat_input("Como podemos ajudar?", disabled=disabled)) and not disabled:
        st.session_state.history.append({"role": "user", "content": prompt})
        _submit_turn(user_message=prompt)


if __name__ == "__main__":
    main()

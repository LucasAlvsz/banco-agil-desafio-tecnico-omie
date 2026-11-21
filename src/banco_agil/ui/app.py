import logging
from decimal import Decimal
from uuid import uuid4

import streamlit as st
from langchain_core.callbacks import BaseCallbackHandler

from banco_agil.agents.graph import (
    LimitIncreaseConfirmation,
    TurnResult,
    build_graph,
    resume_turn,
    run_turn,
)
from banco_agil.agents.state import AgentName
from banco_agil.config import Settings
from banco_agil.domain.services import CreditService
from banco_agil.infrastructure.csv_repositories import (
    CsvClientRepository,
    CsvLimitRequestRepository,
    CsvScoreLimitRepository,
)
from banco_agil.infrastructure.exchange_client import ExchangeRateClient
from banco_agil.infrastructure.llm_factory import create_chat_model
from banco_agil.infrastructure.observability import (
    LangfuseObservability,
    TurnTelemetry,
    create_langfuse_observability,
)
from banco_agil.infrastructure.privacy import configure_logging

logger = logging.getLogger(__name__)


def _format_limit(amount: float) -> str:
    whole, _, fraction = format(Decimal(str(amount)), ",f").partition(".")
    return f"R$ {whole.replace(',', '.')},{fraction.ljust(2, '0')}"


def _record_reply(result: TurnResult) -> None:
    st.session_state.history.append({"role": "assistant", "content": result.reply})
    st.session_state.pending_confirmation = result.pending_confirmation
    st.session_state.conversation_ended = result.conversation_ended
    st.session_state.last_telemetry = result.telemetry
    st.session_state.active_agent = result.active_agent
    st.rerun()


def _show_telemetry(
    telemetry: TurnTelemetry | None, active_agent: AgentName, *, langfuse_enabled: bool
) -> None:
    with st.sidebar:
        st.subheader("Telemetria do turno")
        st.caption("Langfuse ativo" if langfuse_enabled else "Local (Langfuse desativado)")
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
    if new_conversation or "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid4())
        st.session_state.history = []
        st.session_state.conversation_ended = False
        st.session_state.pending_confirmation = None
        st.session_state.last_telemetry = None
        st.session_state.active_agent = "triagem"
        for key in ("graph", "llm_model", "langfuse_observability"):
            st.session_state.pop(key, None)

    try:
        if "graph" not in st.session_state:
            settings = Settings()
            credit_service = CreditService(
                CsvClientRepository(settings.data_dir / "clientes.csv"),
                CsvScoreLimitRepository(settings.data_dir / "score_limite.csv"),
                CsvLimitRequestRepository(settings.data_dir / "solicitacoes_aumento_limite.csv"),
            )
            exchange_provider = ExchangeRateClient(
                timeout_seconds=settings.exchange_timeout_seconds
            )
            st.session_state.llm_model = settings.llm_model
            st.session_state.langfuse_observability = create_langfuse_observability(settings)
            st.session_state.graph = build_graph(
                create_chat_model(settings), credit_service, exchange_provider=exchange_provider
            )
    except Exception as exc:
        logger.error("Falha na inicialização do atendimento (%s)", type(exc).__name__)
        st.error(
            "Não foi possível iniciar o atendimento. Verifique a configuração do provedor LLM."
        )
        st.stop()

    observability: LangfuseObservability | None = st.session_state.langfuse_observability
    callbacks: tuple[BaseCallbackHandler, ...] = (
        (observability.create_handler(),) if observability is not None else ()
    )
    _show_telemetry(
        st.session_state.last_telemetry,
        st.session_state.active_agent,
        langfuse_enabled=observability is not None,
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
            with st.spinner("Aguarde..."):
                result = resume_turn(
                    st.session_state.graph,
                    thread_id=st.session_state.thread_id,
                    approved=confirm and not cancel,
                    callbacks=callbacks,
                    model_name=st.session_state.llm_model,
                )
            _record_reply(result)

    disabled = st.session_state.conversation_ended or pending is not None
    if (prompt := st.chat_input("Como podemos ajudar?", disabled=disabled)) and not disabled:
        st.session_state.history.append({"role": "user", "content": prompt})
        with st.spinner("Aguarde..."):
            result = run_turn(
                st.session_state.graph,
                thread_id=st.session_state.thread_id,
                user_message=prompt,
                callbacks=callbacks,
                model_name=st.session_state.llm_model,
            )
        _record_reply(result)


if __name__ == "__main__":
    main()

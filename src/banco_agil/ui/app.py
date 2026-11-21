import logging
from decimal import Decimal
from uuid import uuid4

import streamlit as st
from langchain_core.callbacks import BaseCallbackHandler

from banco_agil.agents.graph import (
    ERROR_REPLY,
    LimitIncreaseConfirmation,
    TurnResult,
    build_graph,
    resume_turn,
    run_turn,
)
from banco_agil.agents.state import AgentName
from banco_agil.api.schemas import ChatResponse
from banco_agil.config import Settings
from banco_agil.domain.services import CreditService
from banco_agil.infrastructure.csv_repositories import (
    CsvClientRepository,
    CsvLimitRequestRepository,
    CsvScoreLimitRepository,
)
from banco_agil.infrastructure.llm_factory import create_chat_model
from banco_agil.infrastructure.mcp_exchange_client import create_exchange_provider
from banco_agil.infrastructure.observability import (
    LangfuseObservability,
    TurnTelemetry,
    create_langfuse_observability,
)
from banco_agil.infrastructure.privacy import configure_logging
from banco_agil.ui.client import ApiClientError, HttpChatClient

logger = logging.getLogger(__name__)


def _format_limit(amount: float) -> str:
    whole, _, fraction = format(Decimal(str(amount)), ",f").partition(".")
    return f"R$ {whole.replace(',', '.')},{fraction.ljust(2, '0')}"


def _record_reply(result: TurnResult) -> None:
    if isinstance(result, ChatResponse):
        st.session_state.thread_id = result.thread_id
        st.session_state.langfuse_enabled = result.langfuse_enabled
    st.session_state.history.append({"role": "assistant", "content": result.reply})
    st.session_state.pending_confirmation = result.pending_confirmation
    st.session_state.conversation_ended = result.conversation_ended
    st.session_state.last_telemetry = result.telemetry
    st.session_state.active_agent = result.active_agent
    st.rerun()


def _submit_turn(*, user_message: str | None = None, approved: bool | None = None) -> None:
    thread_id: str = st.session_state.thread_id
    http_client: HttpChatClient | None = st.session_state.get("http_client")
    try:
        with st.spinner("Aguarde..."):
            result: TurnResult
            if http_client is not None:
                if user_message is not None:
                    result = http_client.send_message(
                        thread_id=thread_id, user_message=user_message
                    )
                else:
                    assert approved is not None
                    result = http_client.resume(thread_id=thread_id, approved=approved)
            else:
                observability: LangfuseObservability | None = (
                    st.session_state.langfuse_observability
                )
                callbacks: tuple[BaseCallbackHandler, ...] = (
                    (observability.create_handler(),) if observability is not None else ()
                )
                graph = st.session_state.graph
                model_name = st.session_state.llm_model
                if user_message is not None:
                    result = run_turn(
                        graph,
                        thread_id=thread_id,
                        user_message=user_message,
                        callbacks=callbacks,
                        model_name=model_name,
                    )
                else:
                    assert approved is not None
                    result = resume_turn(
                        graph,
                        thread_id=thread_id,
                        approved=approved,
                        callbacks=callbacks,
                        model_name=model_name,
                    )
    except ApiClientError as exc:
        logger.error("Falha na comunicação com a API (%s)", type(exc).__name__)
        st.session_state.history.append({"role": "assistant", "content": ERROR_REPLY})
        st.rerun()
        return

    _record_reply(result)


def _show_telemetry(
    telemetry: TurnTelemetry | None,
    active_agent: AgentName,
    *,
    langfuse_enabled: bool,
    http_mode: bool,
) -> None:
    with st.sidebar:
        st.subheader("Telemetria do turno")
        st.caption("Langfuse ativo" if langfuse_enabled else "Local (Langfuse desativado)")
        st.caption("API HTTP" if http_mode else "Direto (em memória)")
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
        for key in ("graph", "llm_model", "langfuse_observability", "http_client"):
            st.session_state.pop(key, None)

    try:
        if "graph" not in st.session_state and "http_client" not in st.session_state:
            settings = Settings()
            if settings.api_base_url is not None:
                http_client = HttpChatClient(
                    settings.api_base_url, timeout_seconds=settings.api_timeout_seconds
                )
                health = http_client.check_health()
                st.session_state.langfuse_enabled = health is not None and health.langfuse_enabled
                st.session_state.http_client = http_client
            else:
                credit_service = CreditService(
                    CsvClientRepository(settings.data_dir / "clientes.csv"),
                    CsvScoreLimitRepository(settings.data_dir / "score_limite.csv"),
                    CsvLimitRequestRepository(
                        settings.data_dir / "solicitacoes_aumento_limite.csv"
                    ),
                )
                exchange_provider = create_exchange_provider(settings)
                st.session_state.llm_model = settings.llm_model
                st.session_state.langfuse_observability = create_langfuse_observability(settings)
                st.session_state.graph = build_graph(
                    create_chat_model(settings), credit_service, exchange_provider=exchange_provider
                )
                st.session_state.langfuse_enabled = (
                    st.session_state.langfuse_observability is not None
                )
    except Exception as exc:
        logger.error("Falha na inicialização do atendimento (%s)", type(exc).__name__)
        st.error(
            "Não foi possível iniciar o atendimento. Verifique a configuração do provedor LLM."
        )
        st.stop()

    _show_telemetry(
        st.session_state.last_telemetry,
        st.session_state.active_agent,
        langfuse_enabled=st.session_state.langfuse_enabled,
        http_mode="http_client" in st.session_state,
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

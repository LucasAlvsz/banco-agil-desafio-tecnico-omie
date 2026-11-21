from decimal import Decimal
from uuid import uuid4

import streamlit as st

from banco_agil.agents.graph import (
    LimitIncreaseConfirmation,
    TurnResult,
    build_graph,
    resume_turn,
    run_turn,
)
from banco_agil.config import Settings
from banco_agil.domain.services import CreditService
from banco_agil.infrastructure.csv_repositories import (
    CsvClientRepository,
    CsvLimitRequestRepository,
    CsvScoreLimitRepository,
)
from banco_agil.infrastructure.exchange_client import ExchangeRateClient
from banco_agil.infrastructure.llm_factory import create_chat_model


def _format_limit(amount: float) -> str:
    whole, _, fraction = format(Decimal(str(amount)), ",f").partition(".")
    return f"R$ {whole.replace(',', '.')},{fraction.ljust(2, '0')}"


def _record_reply(result: TurnResult) -> None:
    st.session_state.history.append({"role": "assistant", "content": result.reply})
    st.session_state.pending_confirmation = result.pending_confirmation
    st.session_state.conversation_ended = result.conversation_ended
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="Banco Ágil", page_icon="🏦")
    st.title("Banco Ágil")

    new_conversation = st.sidebar.button("Nova conversa")
    if new_conversation or "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid4())
        st.session_state.history = []
        st.session_state.conversation_ended = False
        st.session_state.pending_confirmation = None
        st.session_state.pop("graph", None)

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
            st.session_state.graph = build_graph(
                create_chat_model(settings), credit_service, exchange_provider=exchange_provider
            )
    except Exception:
        st.error(
            "Não foi possível iniciar o atendimento. Verifique a configuração do provedor LLM."
        )
        st.stop()

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
            )
        _record_reply(result)


if __name__ == "__main__":
    main()

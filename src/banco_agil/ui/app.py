from uuid import uuid4

import streamlit as st

from banco_agil.agents.graph import build_graph, run_turn
from banco_agil.config import Settings
from banco_agil.infrastructure.llm_factory import create_chat_model


def main() -> None:
    st.set_page_config(page_title="Banco Ágil", page_icon="🏦")
    st.title("Banco Ágil")

    new_conversation = st.sidebar.button("Nova conversa")
    if new_conversation or "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid4())
        st.session_state.history = []
        st.session_state.conversation_ended = False
        st.session_state.pop("graph", None)

    try:
        if "graph" not in st.session_state:
            st.session_state.graph = build_graph(create_chat_model(Settings()))
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

    if prompt := st.chat_input(
        "Como podemos ajudar?", disabled=st.session_state.conversation_ended
    ):
        st.session_state.history.append({"role": "user", "content": prompt})
        with st.spinner("Aguarde..."):
            result = run_turn(
                st.session_state.graph,
                thread_id=st.session_state.thread_id,
                user_message=prompt,
            )
        st.session_state.history.append({"role": "assistant", "content": result.reply})
        st.session_state.conversation_ended = result.conversation_ended
        st.rerun()


if __name__ == "__main__":
    main()

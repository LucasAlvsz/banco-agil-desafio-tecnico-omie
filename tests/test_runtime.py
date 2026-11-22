from unittest.mock import Mock

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage

from banco_agil import runtime as runtime_module
from banco_agil.config import Settings
from banco_agil.infrastructure.observability import LangfuseObservability, TurnObservability
from banco_agil.runtime import ConversationRuntime


def test_runtime_composes_dependencies_and_flushes_on_reset_and_close(
    monkeypatch, scripted_model, data_dir
):
    tool_calls = [
        {
            "name": "autenticar_cliente",
            "args": {"cpf": "12345678901", "data_nascimento": "14/05/1990"},
            "id": "auth",
        },
        {"name": "redirecionar_para_credito", "args": {}, "id": "credit"},
        {"name": "solicitar_aumento_limite", "args": {"novo_limite": 6000}, "id": "request"},
    ]
    model = scripted_model(
        responses=[
            *(AIMessage(content="", tool_calls=[call]) for call in tool_calls),
            AIMessage(content="Pedido cancelado."),
            AIMessage(content="Olá!"),
        ]
    )
    settings = Settings(data_dir=data_dir, llm_model="openai/gpt-4o-mini")
    model_factory = Mock(return_value=model)
    client = Mock()
    handler_factory = Mock(side_effect=BaseCallbackHandler)
    monkeypatch.setattr(runtime_module, "create_chat_model", model_factory)
    monkeypatch.setattr(
        runtime_module,
        "create_langfuse_observability",
        lambda _: LangfuseObservability(client, handler_factory),
    )

    with ConversationRuntime.from_settings(settings) as runtime:
        assert runtime.langfuse_enabled
        turn = runtime.run_turn(thread_id="thread", user_message="Quero aumentar meu limite.")
        assert turn.pending_confirmation is not None
        assert turn.telemetry.model_name == settings.llm_model
        assert runtime.get_state("thread")["client_cpf"] == "12345678901"
        resumed = runtime.resume_turn(thread_id="thread", approved=False)
        assert resumed.reply == "Pedido cancelado."
        assert resumed.pending_confirmation is None
        assert handler_factory.call_count == 2
        client.flush.assert_not_called()
        assert runtime.run_turn(thread_id="other", user_message="Olá.").reply
        runtime.reset_thread("thread")
        client.flush.assert_called_once_with()
        runtime.reset_thread("thread")
        assert runtime.get_state("thread") == {}
        assert runtime.get_state("other")
        assert client.flush.call_count == 2
        runtime.reset_thread("missing-thread")
        assert runtime.get_state("missing-thread") == {}
        assert client.flush.call_count == 3
    runtime.close()
    assert client.flush.call_count == 4
    model_factory.assert_called_once_with(settings)


def test_runtime_leaves_injected_observability_open(monkeypatch, scripted_model, credit_service):
    client = Mock()
    observer = TurnObservability(LangfuseObservability(client, BaseCallbackHandler))
    create = Mock(side_effect=AssertionError("Unexpected observer initialization"))
    monkeypatch.setattr(runtime_module, "create_langfuse_observability", create)
    with ConversationRuntime.from_settings(
        model=scripted_model(responses=[AIMessage(content="Olá!")]),
        credit_service=credit_service,
        observability=observer,
    ) as runtime:
        assert runtime.run_turn(thread_id="thread", user_message="Oi").reply == "Olá!"
        runtime.reset_thread("thread")
        assert runtime.get_state("thread") == {}
    create.assert_not_called()
    client.flush.assert_not_called()

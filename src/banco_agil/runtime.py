from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from banco_agil.agents.graph import (
    DEFAULT_RECURSION_LIMIT,
    ConversationGraph,
    TurnResult,
    build_graph,
    resume_turn,
    run_turn,
)
from banco_agil.agents.state import ConversationState
from banco_agil.config import Settings
from banco_agil.domain.repositories import ExchangeRateProvider
from banco_agil.domain.services import CreditService
from banco_agil.infrastructure.csv_repositories import (
    CsvClientRepository,
    CsvLimitRequestRepository,
    CsvScoreLimitRepository,
)
from banco_agil.infrastructure.llm_factory import create_chat_model
from banco_agil.infrastructure.mcp_exchange_client import create_exchange_provider
from banco_agil.infrastructure.observability import TurnObservability, create_langfuse_observability


class ConversationRuntime:
    def __init__(
        self,
        graph: ConversationGraph,
        *,
        model_name: str | None = None,
        observability: TurnObservability | None = None,
        owns_observability: bool = False,
    ) -> None:
        self._graph = graph
        self._model_name = model_name
        self._observability = observability if observability is not None else TurnObservability()
        self._owns_observability = owns_observability

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        model: BaseChatModel | None = None,
        credit_service: CreditService | None = None,
        exchange_provider: ExchangeRateProvider | None = None,
        observability: TurnObservability | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> Self:
        settings = settings if settings is not None else Settings()
        if credit_service is None:
            credit_service = CreditService(
                CsvClientRepository(settings.data_dir / "clientes.csv"),
                CsvScoreLimitRepository(settings.data_dir / "score_limite.csv"),
                CsvLimitRequestRepository(settings.data_dir / "solicitacoes_aumento_limite.csv"),
            )
        if exchange_provider is None:
            exchange_provider = create_exchange_provider(settings)
        if model is None:
            model = create_chat_model(settings)
        graph = build_graph(
            model, credit_service, exchange_provider=exchange_provider, checkpointer=checkpointer
        )
        owns_observability = observability is None
        if observability is None:
            observability = TurnObservability(create_langfuse_observability(settings))
        return cls(
            graph,
            model_name=settings.llm_model,
            observability=observability,
            owns_observability=owns_observability,
        )

    @property
    def langfuse_enabled(self) -> bool:
        return self._observability.langfuse_enabled

    def run_turn(
        self,
        *,
        thread_id: str,
        user_message: str,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
        callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> TurnResult:
        return run_turn(
            self._graph,
            thread_id=thread_id,
            user_message=user_message,
            recursion_limit=recursion_limit,
            callbacks=callbacks,
            model_name=self._model_name,
            observability=self._observability,
        )

    def resume_turn(
        self,
        *,
        thread_id: str,
        approved: bool,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
        callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> TurnResult:
        return resume_turn(
            self._graph,
            thread_id=thread_id,
            approved=approved,
            recursion_limit=recursion_limit,
            callbacks=callbacks,
            model_name=self._model_name,
            observability=self._observability,
        )

    def get_state(self, thread_id: str) -> ConversationState:
        snapshot = self._graph.get_state({"configurable": {"thread_id": thread_id}})
        return cast(ConversationState, snapshot.values)

    def reset_thread(self, thread_id: str) -> None:
        checkpointer = self._graph.checkpointer
        if isinstance(checkpointer, BaseCheckpointSaver):
            checkpointer.delete_thread(thread_id)
        if self._owns_observability:
            self._observability.flush()

    def close(self) -> None:
        if self._owns_observability:
            self._owns_observability = False
            self._observability.flush()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

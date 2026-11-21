import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import RequestResponseEndpoint

from banco_agil.agents.graph import ConversationGraph, build_graph, resume_turn, run_turn
from banco_agil.api.schemas import ChatRequest, ChatResponse, HealthResponse
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
    create_langfuse_observability,
)
from banco_agil.infrastructure.privacy import configure_logging

logger = logging.getLogger(__name__)
VALIDATION_LOCATIONS = {"body", *ChatRequest.model_fields}


@dataclass
class Runtime:
    graph: ConversationGraph
    settings: Settings
    observability: LangfuseObservability | None
    thread_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    creation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    workers: set[asyncio.Task[ChatResponse]] = field(default_factory=set)


def create_app(
    *,
    settings: Settings | None = None,
    graph: ConversationGraph | None = None,
    observability: LangfuseObservability | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        runtime_settings = settings if settings is not None else Settings()
        runtime_graph = graph
        runtime_observability = observability
        if runtime_graph is None:
            credit_service = CreditService(
                CsvClientRepository(runtime_settings.data_dir / "clientes.csv"),
                CsvScoreLimitRepository(runtime_settings.data_dir / "score_limite.csv"),
                CsvLimitRequestRepository(
                    runtime_settings.data_dir / "solicitacoes_aumento_limite.csv"
                ),
            )
            exchange_provider = create_exchange_provider(runtime_settings)
            runtime_graph = build_graph(
                create_chat_model(runtime_settings),
                credit_service,
                exchange_provider=exchange_provider,
            )
            if runtime_observability is None:
                runtime_observability = create_langfuse_observability(runtime_settings)

        runtime = Runtime(runtime_graph, runtime_settings, runtime_observability)
        app.state.runtime = runtime
        try:
            yield
        finally:
            await asyncio.gather(*runtime.workers, return_exceptions=True)
            del app.state.runtime
            if runtime_observability is not None:
                runtime_observability.flush()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Extra JSON keys are user input too and can contain personal data.
        errors = [
            {
                "loc": [part for part in error["loc"] if part in VALIDATION_LOCATIONS],
                "msg": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.middleware("http")
    async def handle_errors(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Catch before Starlette's server error middleware can re-raise to the server logger.
        try:
            return await call_next(request)
        except Exception as exc:
            logger.error("Falha ao processar solicitação (%s)", type(exc).__name__)
            return JSONResponse(
                status_code=500, content={"detail": "Erro interno ao processar a solicitação."}
            )

    @app.get("/health")
    async def health(request: Request) -> HealthResponse:
        runtime = cast(Runtime, request.app.state.runtime)
        return HealthResponse(langfuse_enabled=runtime.observability is not None)

    @app.post("/api/v1/chat")
    async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
        runtime = cast(Runtime, request.app.state.runtime)
        thread_id = payload.thread_id if payload.thread_id is not None else str(uuid4())
        async with runtime.creation_lock:
            lock = runtime.thread_locks.get(thread_id)
            if lock is None:
                lock = runtime.thread_locks[thread_id] = asyncio.Lock()

        def execute() -> ChatResponse:
            observer = runtime.observability
            callbacks = (observer.create_handler(),) if observer is not None else ()
            model_name = runtime.settings.llm_model
            if payload.message is not None:
                turn = run_turn(
                    runtime.graph,
                    thread_id=thread_id,
                    user_message=payload.message,
                    callbacks=callbacks,
                    model_name=model_name,
                )
            else:
                assert payload.approve is not None
                turn = resume_turn(
                    runtime.graph,
                    thread_id=thread_id,
                    approved=payload.approve,
                    callbacks=callbacks,
                    model_name=model_name,
                )
            return ChatResponse(
                **turn.model_dump(), thread_id=thread_id, langfuse_enabled=observer is not None
            )

        def finish(worker: asyncio.Task[ChatResponse]) -> None:
            runtime.workers.discard(worker)
            lock.release()
            if not worker.cancelled():
                worker.exception()

        # A cancelled HTTP request cannot stop the thread. Release the lock when the work ends.
        await lock.acquire()
        worker = asyncio.create_task(asyncio.to_thread(execute))
        runtime.workers.add(worker)
        worker.add_done_callback(finish)
        return await asyncio.shield(worker)

    return app


app = create_app()

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

from banco_agil.api.schemas import ChatRequest, ChatResponse, HealthResponse
from banco_agil.config import Settings
from banco_agil.infrastructure.privacy import configure_logging
from banco_agil.runtime import ConversationRuntime

logger = logging.getLogger(__name__)
VALIDATION_LOCATIONS = {"body", *ChatRequest.model_fields}


@dataclass
class ApiRuntimeState:
    conversation: ConversationRuntime
    owns_runtime: bool
    thread_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    creation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    workers: set[asyncio.Task[ChatResponse]] = field(default_factory=set)


def create_app(
    *,
    settings: Settings | None = None,
    runtime: ConversationRuntime | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        conversation = (
            runtime if runtime is not None else ConversationRuntime.from_settings(settings)
        )
        state = ApiRuntimeState(conversation, owns_runtime=runtime is None)
        app.state.runtime = state
        try:
            yield
        finally:
            await asyncio.gather(*state.workers, return_exceptions=True)
            del app.state.runtime
            if state.owns_runtime:
                state.conversation.close()

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
        state = cast(ApiRuntimeState, request.app.state.runtime)
        return HealthResponse(langfuse_enabled=state.conversation.langfuse_enabled)

    @app.post("/api/v1/chat")
    async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
        state = cast(ApiRuntimeState, request.app.state.runtime)
        thread_id = payload.thread_id if payload.thread_id is not None else str(uuid4())
        async with state.creation_lock:
            lock = state.thread_locks.get(thread_id)
            if lock is None:
                lock = state.thread_locks[thread_id] = asyncio.Lock()

        def execute() -> ChatResponse:
            conversation = state.conversation
            if payload.message is not None:
                turn = conversation.run_turn(thread_id=thread_id, user_message=payload.message)
            else:
                assert payload.approve is not None
                turn = conversation.resume_turn(thread_id=thread_id, approved=payload.approve)
            return ChatResponse(
                **turn.model_dump(),
                thread_id=thread_id,
                langfuse_enabled=conversation.langfuse_enabled,
            )

        def finish(worker: asyncio.Task[ChatResponse]) -> None:
            state.workers.discard(worker)
            lock.release()
            if not worker.cancelled():
                worker.exception()

        # A cancelled HTTP request cannot stop the thread. Release the lock when the work ends.
        await lock.acquire()
        worker = asyncio.create_task(asyncio.to_thread(execute))
        state.workers.add(worker)
        worker.add_done_callback(finish)
        return await asyncio.shield(worker)

    return app


app = create_app()

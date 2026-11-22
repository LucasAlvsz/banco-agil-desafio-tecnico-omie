from types import TracebackType
from typing import Protocol, Self

import httpx
from pydantic import BaseModel

from banco_agil.agents.graph import TurnResult
from banco_agil.api.schemas import ChatResponse, HealthResponse
from banco_agil.config import Settings
from banco_agil.runtime import ConversationRuntime


class ChatClientError(Exception):
    pass


class ApiClientError(ChatClientError):
    pass


class ChatClient(Protocol):
    @property
    def mode_label(self) -> str: ...

    @property
    def langfuse_enabled(self) -> bool: ...

    def send_message(self, *, thread_id: str, user_message: str) -> ChatResponse: ...

    def resume(self, *, thread_id: str, approved: bool) -> ChatResponse: ...

    def reset_conversation(self, thread_id: str) -> None: ...

    def close(self) -> None: ...


class HttpChatClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._transport = transport
        self._langfuse_enabled = False

    @property
    def mode_label(self) -> str:
        return "API HTTP"

    @property
    def langfuse_enabled(self) -> bool:
        return self._langfuse_enabled

    def send_message(self, *, thread_id: str, user_message: str) -> ChatResponse:
        return self._send_turn({"thread_id": thread_id, "message": user_message})

    def resume(self, *, thread_id: str, approved: bool) -> ChatResponse:
        return self._send_turn({"thread_id": thread_id, "approve": approved})

    def check_health(self) -> HealthResponse | None:
        try:
            health = self._request("GET", "/health", HealthResponse)
        except ApiClientError:
            self._langfuse_enabled = False
            return None
        self._langfuse_enabled = health.langfuse_enabled
        return health

    def reset_conversation(self, thread_id: str) -> None:
        pass

    def close(self) -> None:
        pass

    def _send_turn(self, payload: dict[str, str | bool]) -> ChatResponse:
        response = self._request("POST", "/api/v1/chat", ChatResponse, payload)
        self._langfuse_enabled = response.langfuse_enabled
        return response

    def _request[ResponseModel: BaseModel](
        self,
        method: str,
        path: str,
        schema: type[ResponseModel],
        payload: dict[str, str | bool] | None = None,
    ) -> ResponseModel:
        try:
            with httpx.Client(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = client.request(method, path, json=payload)
                return schema.model_validate_json(response.raise_for_status().content)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            pass
        raise ApiClientError("Não foi possível comunicar com a API.")


class LocalChatClient:
    def __init__(self, runtime: ConversationRuntime, *, owns_runtime: bool = False) -> None:
        self._runtime = runtime
        self._owns_runtime = owns_runtime

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Self:
        return cls(ConversationRuntime.from_settings(settings), owns_runtime=True)

    @property
    def mode_label(self) -> str:
        return "Direto (em memória)"

    @property
    def langfuse_enabled(self) -> bool:
        return self._runtime.langfuse_enabled

    def send_message(self, *, thread_id: str, user_message: str) -> ChatResponse:
        result = self._runtime.run_turn(thread_id=thread_id, user_message=user_message)
        return self._to_response(result, thread_id)

    def resume(self, *, thread_id: str, approved: bool) -> ChatResponse:
        result = self._runtime.resume_turn(thread_id=thread_id, approved=approved)
        return self._to_response(result, thread_id)

    def _to_response(self, result: TurnResult, thread_id: str) -> ChatResponse:
        return ChatResponse(
            **result.model_dump(), thread_id=thread_id, langfuse_enabled=self.langfuse_enabled
        )

    def reset_conversation(self, thread_id: str) -> None:
        self._runtime.reset_thread(thread_id)

    def close(self) -> None:
        if self._owns_runtime:
            self._owns_runtime = False
            self._runtime.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def create_chat_client(settings: Settings | None = None) -> ChatClient:
    resolved = settings if settings is not None else Settings()
    if resolved.api_base_url is not None:
        client = HttpChatClient(resolved.api_base_url, timeout_seconds=resolved.api_timeout_seconds)
        client.check_health()
        return client
    return LocalChatClient.from_settings(resolved)

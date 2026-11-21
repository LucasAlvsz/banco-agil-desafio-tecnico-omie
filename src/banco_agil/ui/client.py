import httpx
from pydantic import BaseModel

from banco_agil.api.schemas import ChatResponse, HealthResponse


class ApiClientError(Exception):
    pass


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

    def send_message(self, *, thread_id: str, user_message: str) -> ChatResponse:
        return self._request(
            "POST", "/api/v1/chat", ChatResponse, {"thread_id": thread_id, "message": user_message}
        )

    def resume(self, *, thread_id: str, approved: bool) -> ChatResponse:
        return self._request(
            "POST", "/api/v1/chat", ChatResponse, {"thread_id": thread_id, "approve": approved}
        )

    def check_health(self) -> HealthResponse | None:
        try:
            return self._request("GET", "/health", HealthResponse)
        except ApiClientError:
            return None

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

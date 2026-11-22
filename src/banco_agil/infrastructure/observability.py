import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from math import isfinite
from time import perf_counter
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from pydantic import BaseModel, ConfigDict, Field

from banco_agil.config import Settings
from banco_agil.infrastructure.privacy import mask_pii, mask_pii_data

if TYPE_CHECKING:
    from langfuse import Langfuse
    from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult

logger = logging.getLogger(__name__)


class TurnTelemetry(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    latency_ms: float = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    model_name: str | None = None
    agent_route: tuple[str, ...] = ()
    tools_called: tuple[str, ...] = ()
    guardrail_blocked: bool = False


def estimate_cost_usd(
    model_name: str | None, input_tokens: int, output_tokens: int
) -> float | None:
    if input_tokens == output_tokens == 0:
        return 0.0
    if not model_name:
        return None

    try:
        from litellm import cost_per_token

        prompt, completion = cost_per_token(
            model=model_name, prompt_tokens=input_tokens, completion_tokens=output_tokens
        )
        cost = prompt + completion
        return cost if isfinite(cost) and cost >= 0 else None
    except Exception:
        return None


def _token_count(value: object, default: int = 0) -> int:
    return value if type(value) is int and value >= 0 else default


class TurnTelemetryCallback(BaseCallbackHandler):
    def __init__(
        self, tracked_nodes: Collection[str], default_model_name: str | None = None
    ) -> None:
        self._tracked_nodes = tracked_nodes
        self._model_name = default_model_name
        self.agent_route: list[str] = []
        self.tools_called: list[str] = []
        self.input_tokens = self.output_tokens = self.total_tokens = 0

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node = (metadata or {}).get("langgraph_node")
        if (
            isinstance(node, str)
            and node in self._tracked_nodes
            and (not self.agent_route or self.agent_route[-1] != node)
        ):
            self.agent_route.append(node)

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
        name = kwargs.get("name") or serialized.get("name")
        if isinstance(name, str):
            self.tools_called.append(mask_pii(name))

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        metadata = response.llm_output or {}
        usage = metadata.get("token_usage")
        # Alternatives in one generation group share the request's token usage.
        generation = next((group[0] for group in response.generations if group), None)
        if isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage):
            message = generation.message
            metadata = metadata | message.response_metadata
            message_usage = message.response_metadata.get("token_usage")
            if message.usage_metadata is not None:
                usage = message.usage_metadata
            elif isinstance(message_usage, Mapping):
                usage = message_usage

        model = metadata.get("model_name") or metadata.get("model")
        if isinstance(model, str) and model:
            self._model_name = model
        if isinstance(usage, Mapping):
            input_tokens = _token_count(usage.get("input_tokens", usage.get("prompt_tokens")))
            output_tokens = _token_count(usage.get("output_tokens", usage.get("completion_tokens")))
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.total_tokens += _token_count(
                usage.get("total_tokens"), input_tokens + output_tokens
            )

    def build(
        self, latency_ms: float, *, fallback_node: str, guardrail_blocked: bool
    ) -> TurnTelemetry:
        return TurnTelemetry(
            latency_ms=latency_ms,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            estimated_cost_usd=estimate_cost_usd(
                self._model_name, self.input_tokens, self.output_tokens
            ),
            model_name=mask_pii(self._model_name) if self._model_name else None,
            agent_route=tuple(self.agent_route) or (fallback_node,),
            tools_called=tuple(self.tools_called),
            guardrail_blocked=guardrail_blocked,
        )


def langfuse_mask_data(*, data: Any, **kwargs: Any) -> Any:
    return mask_pii_data(data)


def langfuse_mask_otel_spans(*, params: "MaskOtelSpansParams") -> "MaskOtelSpansResult":
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        attributes = {}
        deleted = []
        for key, value in span.attributes.items():
            if mask_pii(key) != key:
                deleted.append(key)
                continue
            masked = mask_pii(value) if isinstance(value, str) else mask_pii_data(value)
            if masked != value:
                attributes[key] = masked
        if attributes or deleted:
            patches[identifier] = OtelSpanPatch(
                set_attributes=attributes, delete_attributes=tuple(deleted)
            )
    return MaskOtelSpansResult(span_patches=patches)


class LangfuseObservability:
    def __init__(
        self, client: "Langfuse", handler_factory: Callable[[], BaseCallbackHandler]
    ) -> None:
        self._client = client
        self._handler_factory = handler_factory

    def create_handler(self) -> BaseCallbackHandler:
        return self._handler_factory()

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            logger.warning("Langfuse indisponível (%s)", type(exc).__name__)


def is_langfuse_configured(settings: Settings) -> bool:
    return all(
        key is not None and key.get_secret_value().strip()
        for key in (settings.langfuse_public_key, settings.langfuse_secret_key)
    )


def create_langfuse_observability(settings: Settings) -> LangfuseObservability | None:
    if not is_langfuse_configured(settings):
        return None
    assert settings.langfuse_public_key is not None and settings.langfuse_secret_key is not None
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        public_key = settings.langfuse_public_key.get_secret_value()
        client = Langfuse(
            public_key=public_key,
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url
            or settings.langfuse_host
            or "https://cloud.langfuse.com",
            mask=langfuse_mask_data,
            mask_otel_spans=langfuse_mask_otel_spans,
        )
        handler_factory = partial(CallbackHandler, public_key=public_key)
        handler_factory()
        return LangfuseObservability(client, handler_factory)
    except Exception as exc:
        logger.warning("Langfuse indisponível (%s)", type(exc).__name__)
        return None


@dataclass(frozen=True)
class TurnScope:
    callbacks: tuple[BaseCallbackHandler, ...]
    metadata: Mapping[str, Any]
    _telemetry: TurnTelemetryCallback
    _started: float

    def finish(self, *, fallback_node: str, guardrail_blocked: bool = False) -> TurnTelemetry:
        return self._telemetry.build(
            (perf_counter() - self._started) * 1000,
            fallback_node=fallback_node,
            guardrail_blocked=guardrail_blocked,
        )


class TurnObservability:
    def __init__(self, langfuse: LangfuseObservability | None = None) -> None:
        self._langfuse = langfuse

    @property
    def langfuse_enabled(self) -> bool:
        return self._langfuse is not None

    def start_turn(
        self,
        *,
        thread_id: str,
        tracked_nodes: Collection[str],
        default_model_name: str | None = None,
        extra_callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> TurnScope:
        started = perf_counter()
        telemetry = TurnTelemetryCallback(tracked_nodes, default_model_name)
        callbacks: tuple[BaseCallbackHandler, ...] = (telemetry,)
        metadata: dict[str, Any] = {}
        if self._langfuse is not None:
            callbacks += (self._langfuse.create_handler(),)
            metadata = {"langfuse_session_id": thread_id, "langfuse_tags": ["banco-agil"]}
        return TurnScope((*callbacks, *extra_callbacks), metadata, telemetry, started)

    def flush(self) -> None:
        if self._langfuse is not None:
            self._langfuse.flush()

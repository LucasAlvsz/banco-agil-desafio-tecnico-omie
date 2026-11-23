import logging
import sys
from types import TracebackType
from typing import Annotated, Any, Protocol, Self

from mcp import StdioServerParameters
from mcp.client import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ListToolsResult, TextContent
from pydantic import BeforeValidator

from banco_agil.config import Settings
from banco_agil.domain.errors import DomainValidationError, ExchangeServiceError
from banco_agil.domain.models import ExchangeQuote
from banco_agil.domain.repositories import ExchangeRateProvider
from banco_agil.infrastructure.exchange_client import ExchangeRateClient
from banco_agil.infrastructure.privacy import mask_pii

logger = logging.getLogger(__name__)
_QUOTE_TOOL = "consultar_cotacao_cambio"
_INVALID_CURRENCY_PREFIX = "INVALID_CURRENCY:"
_EXCHANGE_UNAVAILABLE_PREFIX = "EXCHANGE_UNAVAILABLE:"
_UNAVAILABLE_MESSAGE = "Não foi possível consultar a cotação no momento."
_TOOL_ERROR_PREFIX = f"Error executing tool {_QUOTE_TOOL}: "

# Let the domain reject non-strings without echoing the input in an SDK validation error.
_CurrencyInput = Annotated[
    str, BeforeValidator(lambda value: value if isinstance(value, str) else "")
]


def _encode_validation_error(exc: DomainValidationError) -> ToolError:
    return ToolError(f"{_INVALID_CURRENCY_PREFIX} {exc}")


def _encode_unavailable_error() -> ToolError:
    return ToolError(f"{_EXCHANGE_UNAVAILABLE_PREFIX} {_UNAVAILABLE_MESSAGE}")


def _decode_quote_result(result: CallToolResult) -> ExchangeQuote:
    if result.is_error:
        match result.content:
            case [TextContent(text=message)]:
                message = message.removeprefix(_TOOL_ERROR_PREFIX)
                if message.startswith(_INVALID_CURRENCY_PREFIX):
                    guidance = message.removeprefix(_INVALID_CURRENCY_PREFIX).strip()
                    raise DomainValidationError(mask_pii(guidance))
        raise ExchangeServiceError(_UNAVAILABLE_MESSAGE)
    if result.structured_content is not None:
        return ExchangeQuote.model_validate(result.structured_content)
    match result.content:
        case [TextContent(text=body)]:
            return ExchangeQuote.model_validate_json(body)
    raise ValueError("Resposta MCP sem cotação")


def create_exchange_mcp_server(
    exchange_provider: ExchangeRateProvider | None = None, *, timeout_seconds: float = 8.0
) -> MCPServer:
    provider = (
        exchange_provider
        if exchange_provider is not None
        else ExchangeRateClient(timeout_seconds=timeout_seconds)
    )
    server = MCPServer("banco-agil-cambio")

    @server.tool(name=_QUOTE_TOOL)
    async def consultar_cotacao_cambio(moeda: _CurrencyInput = "USD") -> ExchangeQuote:
        """Consulta em reais a cotação da moeda informada. O padrão é USD."""
        try:
            return await provider.get_quote(moeda)
        except DomainValidationError as exc:
            raise _encode_validation_error(exc) from None
        except Exception as exc:
            logger.error("Falha no servidor MCP de câmbio (%s)", type(exc).__name__)
            raise _encode_unavailable_error() from None

    return server


type _McpEndpoint = MCPServer | StdioServerParameters | str


class _McpClient(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None: ...

    async def list_tools(self) -> ListToolsResult: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult: ...


class _McpClientFactory(Protocol):
    def __call__(
        self, endpoint: _McpEndpoint, /, *, read_timeout_seconds: float | None = None
    ) -> _McpClient: ...


class McpExchangeRateProvider:
    def __init__(
        self,
        endpoint: _McpEndpoint,
        read_timeout_seconds: float | None = None,
        *,
        client_factory: _McpClientFactory = Client,
    ) -> None:
        self._endpoint = endpoint
        self._read_timeout_seconds = read_timeout_seconds
        self._client_factory = client_factory

    @classmethod
    def in_memory(
        cls,
        exchange_provider: ExchangeRateProvider | None = None,
        *,
        timeout_seconds: float = 8.0,
        client_factory: _McpClientFactory = Client,
    ) -> Self:
        return cls(
            create_exchange_mcp_server(exchange_provider, timeout_seconds=timeout_seconds),
            client_factory=client_factory,
        )

    @classmethod
    def stdio(cls, *, timeout_seconds: float, client_factory: _McpClientFactory = Client) -> Self:
        return cls(
            StdioServerParameters(
                command=sys.executable,
                args=["-m", "banco_agil.mcp.exchange_server", "--timeout", str(timeout_seconds)],
            ),
            read_timeout_seconds=2 * timeout_seconds,
            client_factory=client_factory,
        )

    @classmethod
    def streamable_http(
        cls, *, url: str, timeout_seconds: float, client_factory: _McpClientFactory = Client
    ) -> Self:
        return cls(url, read_timeout_seconds=2 * timeout_seconds, client_factory=client_factory)

    async def get_quote(self, currency: str = "USD") -> ExchangeQuote:
        if isinstance(currency, str) and mask_pii(currency) != currency:
            currency = ""
        try:
            async with self._client_factory(
                self._endpoint, read_timeout_seconds=self._read_timeout_seconds
            ) as client:
                listing = await client.list_tools()
                if not any(tool.name == _QUOTE_TOOL for tool in listing.tools):
                    raise LookupError(_QUOTE_TOOL)
                result = await client.call_tool(_QUOTE_TOOL, {"moeda": currency})
            return _decode_quote_result(result)
        except DomainValidationError:
            raise
        except Exception as exc:
            logger.error("Falha na chamada MCP de câmbio (%s)", type(exc).__name__)
            raise ExchangeServiceError(_UNAVAILABLE_MESSAGE) from None


def create_exchange_provider(
    settings: Settings, *, client_factory: _McpClientFactory = Client
) -> ExchangeRateProvider:
    timeout_seconds = settings.exchange_timeout_seconds
    if settings.mcp_exchange_transport == "stdio":
        return McpExchangeRateProvider.stdio(
            timeout_seconds=timeout_seconds, client_factory=client_factory
        )
    if settings.mcp_exchange_transport == "streamable_http":
        return McpExchangeRateProvider.streamable_http(
            url=settings.resolve_mcp_exchange_url(),
            timeout_seconds=timeout_seconds,
            client_factory=client_factory,
        )
    return McpExchangeRateProvider.in_memory(
        timeout_seconds=timeout_seconds, client_factory=client_factory
    )

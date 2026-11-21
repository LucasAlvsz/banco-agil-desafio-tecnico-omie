import logging
import sys
from typing import Self

from mcp import StdioServerParameters
from mcp.client import Client
from mcp.server import MCPServer
from mcp.types import TextContent

from banco_agil.config import Settings
from banco_agil.domain.errors import DomainValidationError, ExchangeServiceError
from banco_agil.domain.models import ExchangeQuote
from banco_agil.domain.repositories import ExchangeRateProvider
from banco_agil.infrastructure.privacy import mask_pii
from banco_agil.mcp.exchange_server import (
    INVALID_CURRENCY,
    QUOTE_TOOL,
    UNAVAILABLE_MESSAGE,
    create_exchange_mcp_server,
)

logger = logging.getLogger(__name__)


class McpExchangeRateProvider:
    def __init__(
        self,
        endpoint: MCPServer | StdioServerParameters | str,
        read_timeout_seconds: float | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._read_timeout_seconds = read_timeout_seconds

    @classmethod
    def in_memory(
        cls, exchange_provider: ExchangeRateProvider | None = None, *, timeout_seconds: float = 8.0
    ) -> Self:
        return cls(create_exchange_mcp_server(exchange_provider, timeout_seconds=timeout_seconds))

    @classmethod
    def stdio(cls, *, timeout_seconds: float) -> Self:
        return cls(
            StdioServerParameters(
                command=sys.executable,
                args=["-m", "banco_agil.mcp.exchange_server", "--timeout", str(timeout_seconds)],
            ),
            read_timeout_seconds=2 * timeout_seconds,
        )

    @classmethod
    def streamable_http(cls, *, url: str, timeout_seconds: float) -> Self:
        return cls(url, read_timeout_seconds=2 * timeout_seconds)

    async def get_quote(self, currency: str = "USD") -> ExchangeQuote:
        if isinstance(currency, str) and mask_pii(currency) != currency:
            currency = ""
        try:
            async with Client(
                self._endpoint, read_timeout_seconds=self._read_timeout_seconds
            ) as client:
                listing = await client.list_tools()
                if not any(tool.name == QUOTE_TOOL for tool in listing.tools):
                    raise LookupError(QUOTE_TOOL)
                result = await client.call_tool(QUOTE_TOOL, {"moeda": currency})

            if result.is_error:
                match result.content:
                    case [TextContent(text=message)]:
                        message = message.removeprefix(f"Error executing tool {QUOTE_TOOL}: ")
                        if message.startswith(INVALID_CURRENCY):
                            guidance = message.removeprefix(INVALID_CURRENCY).strip()
                            raise DomainValidationError(mask_pii(guidance))
                raise ExchangeServiceError(UNAVAILABLE_MESSAGE)
            if result.structured_content is not None:
                return ExchangeQuote.model_validate(result.structured_content)
            match result.content:
                case [TextContent(text=body)]:
                    return ExchangeQuote.model_validate_json(body)
            raise ValueError("Resposta MCP sem cotação")
        except DomainValidationError:
            raise
        except Exception as exc:
            logger.error("Falha na chamada MCP de câmbio (%s)", type(exc).__name__)
            raise ExchangeServiceError(UNAVAILABLE_MESSAGE) from None


def create_exchange_provider(settings: Settings) -> ExchangeRateProvider:
    timeout_seconds = settings.exchange_timeout_seconds
    if settings.mcp_exchange_transport == "stdio":
        return McpExchangeRateProvider.stdio(timeout_seconds=timeout_seconds)
    if settings.mcp_exchange_transport == "streamable_http":
        url = settings.mcp_exchange_url
        if url is None:
            raise ValueError("Configure MCP_EXCHANGE_URL para usar streamable_http.")
        return McpExchangeRateProvider.streamable_http(url=url, timeout_seconds=timeout_seconds)
    return McpExchangeRateProvider.in_memory(timeout_seconds=timeout_seconds)

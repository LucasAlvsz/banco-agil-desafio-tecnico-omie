import argparse
import logging
import math
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BeforeValidator

from banco_agil.config import Settings
from banco_agil.domain.errors import DomainValidationError
from banco_agil.domain.models import ExchangeQuote
from banco_agil.domain.repositories import ExchangeRateProvider
from banco_agil.infrastructure.exchange_client import ExchangeRateClient
from banco_agil.infrastructure.privacy import configure_logging

logger = logging.getLogger(__name__)
QUOTE_TOOL = "consultar_cotacao_cambio"
INVALID_CURRENCY = "INVALID_CURRENCY:"
UNAVAILABLE_MESSAGE = "Não foi possível consultar a cotação no momento."

# Let the domain reject non-strings without echoing the input in an SDK validation error.
CurrencyInput = Annotated[
    str, BeforeValidator(lambda value: value if isinstance(value, str) else "")
]


def create_exchange_mcp_server(
    exchange_provider: ExchangeRateProvider | None = None, *, timeout_seconds: float = 8.0
) -> MCPServer:
    provider = (
        exchange_provider
        if exchange_provider is not None
        else ExchangeRateClient(timeout_seconds=timeout_seconds)
    )
    server = MCPServer("banco-agil-cambio")

    @server.tool(name=QUOTE_TOOL)
    async def consultar_cotacao_cambio(moeda: CurrencyInput = "USD") -> ExchangeQuote:
        """Consulta em reais a cotação da moeda informada. O padrão é USD."""
        try:
            return await provider.get_quote(moeda)
        except DomainValidationError as exc:
            raise ToolError(f"{INVALID_CURRENCY} {exc}") from None
        except Exception as exc:
            logger.error("Falha no servidor MCP de câmbio (%s)", type(exc).__name__)
            raise ToolError(f"EXCHANGE_UNAVAILABLE: {UNAVAILABLE_MESSAGE}") from None

    return server


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Servidor MCP de câmbio do Banco Ágil")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--timeout", type=float, help="Timeout por provedor externo, em segundos")
    parser.add_argument("--allowed-host", action="append", default=[])
    args = parser.parse_args()

    timeout = args.timeout
    if timeout is None:
        timeout = Settings().exchange_timeout_seconds
    elif not math.isfinite(timeout) or timeout <= 0:
        parser.error("--timeout deve ser um número positivo e finito")
    server = create_exchange_mcp_server(timeout_seconds=timeout)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *args.allowed_host],
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    main()

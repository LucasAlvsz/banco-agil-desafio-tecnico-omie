import argparse
import math

from mcp.server.transport_security import TransportSecuritySettings

from banco_agil.config import Settings
from banco_agil.infrastructure.privacy import configure_logging
from banco_agil.mcp.exchange import create_exchange_mcp_server


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

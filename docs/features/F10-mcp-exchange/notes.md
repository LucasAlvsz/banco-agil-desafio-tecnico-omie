# Notas — F10: Servidor MCP de Câmbio e Fronteira de Segurança

## Entregue
- Servidor MCP stateless e sem PII (`src/banco_agil/mcp/exchange_server.py`, alvo `make mcp-exchange`) construído com `MCPServer` do SDK oficial `mcp>=2.0.0,<3`, expondo exclusivamente `consultar_cotacao_cambio(moeda="USD")` sobre `ExchangeRateClient` (AwesomeAPI + fallback Frankfurter/BCE), com `CurrencyInput` evitando eco de entradas não-string, `ToolError` controlado e CLI suportando `stdio` e `streamable-http` com `TransportSecuritySettings` (proteção contra DNS rebinding ativa com allowlist explícita).
- Cliente `McpExchangeRateProvider` (`src/banco_agil/infrastructure/mcp_exchange_client.py`) implementando o contrato de domínio `ExchangeRateProvider` via `mcp.client.Client` oficial nos transportes `inmemory` (padrão local), `stdio` e `streamable_http`, com descoberta `list_tools()`, saneamento pré-envio de PII, revalidação de `ExchangeQuote` e tradução controlada de erros MCP para `DomainValidationError`/`ExchangeServiceError`.
- Integração via `create_exchange_provider(settings)` em `graph.py`, `api/app.py`, `ui/app.py` e `evals/runner.py`, mantendo o gate de autenticação `ToolRuntime` na ferramenta local `consultar_cotacao` e todas as ferramentas autenticadas/HITL locais no grafo.
- Isolamento físico no Docker: remoção de `data/` da imagem compartilhada (`Dockerfile`/`.dockerignore`) e adição do serviço `mcp-exchange` no `docker-compose.yml` sem volume `./data`, sem porta publicada no host e sem credenciais de LLM/Langfuse, consumido pela `api` via `streamable_http`.
- Bloqueio de `subprocess.Popen` em `tests/conftest.py` e suíte `tests/test_mcp_exchange.py` cobrindo schema, fluxos primário/fallback ponta a ponta, sanitização de PII, erros MCP, transportes e CLI.
- Fronteira de segurança, justificativa documentada do uso do `Client` oficial frente à incompatibilidade de `langchain-mcp-adapters` 0.3.x com `mcp` v2 e caminhos de escala registrados em [ADR 009](../../adr/009-servidor-mcp-cambio-e-fronteira-de-seguranca.md).

## Revisão
- Cross-model review aprovado de primeira (LGTM), sem ajustes de código necessários.

## Verificação
- `make check`: `ruff`, `mypy --strict` (34 arquivos) e 438 testes passando (99% cobertura total; 100% em `exchange_server.py`, `mcp_exchange_client.py`, `config.py`, `graph.py` e `api/app.py`).
- `make evals`: 12/12 cenários aprovados (100%), rotas 100%, 0 vazamentos de PII (exercitando o protocolo MCP em memória).
- `docker compose config --quiet`, `docker build` e verificação de ausência de `/app/data` na imagem compartilhada verdes.

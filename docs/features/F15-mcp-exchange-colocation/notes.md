# Notas — F15: Colocalização do Protocolo MCP de Câmbio

## Entregue
- **Resolução única de `MCP_EXCHANGE_URL` (`src/banco_agil/config.py`):**
  - Adicionado `Settings.resolve_mcp_exchange_url() -> str` como fonte única da regra e da mensagem de erro (`"Configure MCP_EXCHANGE_URL para usar streamable_http."`), protegendo também instâncias criadas via `Settings.model_construct(...)`.
  - Atualizado `@model_validator(mode="after") def require_mcp_exchange_url` para delegar a `resolve_mcp_exchange_url()` quando `mcp_exchange_transport == "streamable_http"`.
- **Módulo profundo de câmbio MCP (`src/banco_agil/mcp/exchange.py`):**
  - Colocalizados no mesmo módulo as constantes privadas do protocolo de fio (`_QUOTE_TOOL`, `_INVALID_CURRENCY_PREFIX`, `_EXCHANGE_UNAVAILABLE_PREFIX`, `_UNAVAILABLE_MESSAGE`, `_TOOL_ERROR_PREFIX`, `_CurrencyInput`), os helpers pareados (`_encode_validation_error`, `_encode_unavailable_error`, `_decode_quote_result`), `create_exchange_mcp_server`, `McpExchangeRateProvider` e `create_exchange_provider` atrás do contrato `ExchangeRateProvider`.
  - Introduzida a costura estrutural privada `_McpEndpoint` / `_McpClient` / `_McpClientFactory` (satisfeita diretamente por `mcp.client.Client` sem `cast` nem wrapper) e o parâmetro opcional `client_factory` nos construtores e fábricas.
  - Preservado o mascaramento preventivo de Dados Pessoais Sensíveis (PII) em `get_quote()` e o registro seguro apenas de `type(exc).__name__` (sem `exc_info`), mantendo integralmente os ADRs 005 e 009.
- **Fachada pública estreita e CLI fina (`src/banco_agil/mcp/__init__.py` e `src/banco_agil/mcp/exchange_server.py`):**
  - Exposta explicitamente em `banco_agil.mcp.__all__` apenas a superfície pública (`McpExchangeRateProvider`, `create_exchange_mcp_server`, `create_exchange_provider`), mantendo constantes e codec privados.
  - Reduzido `src/banco_agil/mcp/exchange_server.py` ao entrypoint CLI fino (`python -m banco_agil.mcp.exchange_server`), preservando `--timeout` positivo/finito e `TransportSecuritySettings` (proteção contra DNS rebinding e `allowed_hosts`).
- **Remoção do módulo legado e migração de importadores:**
  - Removido `src/banco_agil/infrastructure/mcp_exchange_client.py` sem shim de compatibilidade.
  - Migrados `src/banco_agil/runtime.py`, `src/banco_agil/evals/runner.py`, `tests/test_ui.py`, `tests/test_walking_skeleton.py` e `tests/test_mcp_exchange.py` para `banco_agil.mcp`.
- **Testes (*replace, don't layer*):**
  - Eliminados todos os `monkeypatch` globais sobre `mcp.client.Client` e seus métodos em `tests/test_mcp_exchange.py`, substituídos por injeção via `client_factory` (`FakeMcpClient` e spy de transporte).
  - Adicionados testes para `Settings.resolve_mcp_exchange_url()`, fachada pública `banco_agil.mcp.__all__`, remoção do módulo legado e ausência de dependência de `banco_agil.mcp` em `agents/graph.py`.

## Revisão
- `/code-review` em dois eixos (Standards e Spec) sobre `main...4e0456d`: 0 violações de padrões documentados em Standards e 0 lacunas, desvios ou escopo extra em Spec (nenhuma correção necessária).

## Verificação
- `make check`: `ruff check`, `ruff format --check`, `mypy src` (`--strict`, 35 arquivos) e 481 testes passando (99% cobertura total; 100% em `config.py`, `mcp/__init__.py`, `mcp/exchange.py`, `mcp/exchange_server.py` e `runtime.py`).
- `make evals`: 12/12 cenários aprovados (100%), rotas 100%, 0 vazamentos de PII.

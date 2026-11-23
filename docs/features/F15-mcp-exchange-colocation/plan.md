# Plano — F15: Colocalização do Protocolo MCP de Câmbio

## Objetivo

Colocalizar cliente e servidor do protocolo MCP de **Cotação de Câmbio** sob `src/banco_agil/mcp/` atrás do contrato de domínio `ExchangeRateProvider`, conforme o **Candidato 5** da revisão arquitetural (`architecture-review-20261121-080832.html`), preservando integralmente o [ADR 005](../../adr/005-integracao-cambio-com-fallback.md) e o [ADR 009](../../adr/009-servidor-mcp-cambio-e-fronteira-de-seguranca.md).

A mudança resolve quatro dores concretas da arquitetura atual:

1. **Protocolo de fio dividido em dois pacotes:** `src/banco_agil/infrastructure/mcp_exchange_client.py` importa 4 símbolos de `src/banco_agil/mcp/exchange_server.py` (`QUOTE_TOOL`, `INVALID_CURRENCY`, `UNAVAILABLE_MESSAGE`, `create_exchange_mcp_server`), mantendo detalhes privados de fio públicos apenas para viabilizar o acoplamento entre pacotes.
2. **Codificação e decodificação de erros desacopladas:** o servidor codifica `INVALID_CURRENCY:` e `EXCHANGE_UNAVAILABLE:` (inline) em `ToolError`, enquanto o cliente remove manualmente o prefixo `"Error executing tool consultar_cotacao_cambio: "` e interpreta `INVALID_CURRENCY:` em outro pacote.
3. **Validação duplicada de `MCP_EXCHANGE_URL`:** `create_exchange_provider` repete a checagem `if url is None: raise ValueError(...)` e a mensagem de erro já existentes em `Settings.require_mcp_exchange_url` (`src/banco_agil/config.py`) apenas para estreitar o tipo `str | None` sob `mypy --strict`.
4. **Monkeypatch global da classe `mcp.client.Client` nos testes:** `tests/test_mcp_exchange.py` precisa sobrescrever o símbolo `Client` no módulo e os métodos `Client.list_tools` / `Client.call_tool` diretamente na classe global do SDK MCP para testar transportes e respostas malformadas.

## Escopo

### 1. Resolução Única de `MCP_EXCHANGE_URL` (`src/banco_agil/config.py`)

- Adicionar o método `resolve_mcp_exchange_url(self) -> str` em `Settings`:
  - Se `self.mcp_exchange_url is None` ou `not self.mcp_exchange_url.strip()`: levantar `ValueError("Configure MCP_EXCHANGE_URL para usar streamable_http.")`.
  - Caso contrário: retornar `self.mcp_exchange_url`.
- Atualizar `@model_validator(mode="after") def require_mcp_exchange_url(self) -> Self` para chamar `self.resolve_mcp_exchange_url()` quando `self.mcp_exchange_transport == "streamable_http"`.
- Resultado: regra e mensagem em fonte única de verdade, `mypy --strict` satisfeito sem `cast` nem `assert`, e proteção preservada mesmo se `Settings.model_construct(...)` for utilizado.

### 2. Módulo Profundo de Câmbio MCP (`src/banco_agil/mcp/exchange.py`)

Colocalizar protocolo privado, servidor, cliente e fábrica de provedor em um único módulo coeso:

- **Constantes e codec privados do protocolo de fio (prefixo `_`):**
  - `_QUOTE_TOOL = "consultar_cotacao_cambio"`
  - `_INVALID_CURRENCY_PREFIX = "INVALID_CURRENCY:"`
  - `_EXCHANGE_UNAVAILABLE_PREFIX = "EXCHANGE_UNAVAILABLE:"`
  - `_UNAVAILABLE_MESSAGE = "Não foi possível consultar a cotação no momento."`
  - `_TOOL_ERROR_PREFIX = f"Error executing tool {_QUOTE_TOOL}: "`
  - `_CurrencyInput = Annotated[str, BeforeValidator(lambda value: value if isinstance(value, str) else "")]` (evitando eco de entradas não-string em erros de validação do SDK).
  - Helpers privados pareados:
    - `_encode_validation_error(exc: DomainValidationError) -> ToolError`: formata `ToolError(f"{_INVALID_CURRENCY_PREFIX} {exc}")`.
    - `_encode_unavailable_error() -> ToolError`: formata `ToolError(f"{_EXCHANGE_UNAVAILABLE_PREFIX} {_UNAVAILABLE_MESSAGE}")`.
    - `_decode_quote_result(result: CallToolResult) -> ExchangeQuote`:
      - Quando `result.is_error`: extrai texto de `[TextContent(text=message)]`, remove `_TOOL_ERROR_PREFIX`, e se começar com `_INVALID_CURRENCY_PREFIX`, levanta `DomainValidationError(mask_pii(guidance))`; demais erros levantam `ExchangeServiceError(_UNAVAILABLE_MESSAGE)`.
      - Quando `result.structured_content is not None`: valida com `ExchangeQuote.model_validate(result.structured_content)`.
      - Quando `result.content` é `[TextContent(text=body)]`: valida com `ExchangeQuote.model_validate_json(body)`.
      - Caso contrário: levanta `ValueError("Resposta MCP sem cotação")`.
- **Servidor MCP stateless (`create_exchange_mcp_server`):**
  - Preservar a assinatura `create_exchange_mcp_server(exchange_provider: ExchangeRateProvider | None = None, *, timeout_seconds: float = 8.0) -> MCPServer`.
  - Manter o nome do servidor `"banco-agil-cambio"` e a ferramenta `@server.tool(name=_QUOTE_TOOL)` (`consultar_cotacao_cambio(moeda: _CurrencyInput = "USD") -> ExchangeQuote`).
  - Converter `DomainValidationError` via `_encode_validation_error(exc)` e qualquer outra exceção via log seguro (`logger.error("Falha no servidor MCP de câmbio (%s)", type(exc).__name__)`, sem PII nem `exc_info`) + `_encode_unavailable_error()`.
- **Costura interna de fábrica de cliente MCP:**
  - Definir tipos estruturais internos (privados ao módulo, não expostos na fachada pública):
    - `type _McpEndpoint = MCPServer | StdioServerParameters | str`
    - `class _McpClient(Protocol)`: `__aenter__`, `__aexit__`, `list_tools() -> ListToolsResult`, `call_tool(name: str, arguments: dict[str, Any] | None = None) -> CallToolResult`.
    - `class _McpClientFactory(Protocol)`: `__call__(self, endpoint: _McpEndpoint, *, read_timeout_seconds: float | None = None) -> _McpClient`.
  - `mcp.client.Client` satisfaz `_McpClientFactory` diretamente (sem wrapper nem `cast` em produção).
- **Cliente `McpExchangeRateProvider` (atrás de `ExchangeRateProvider`):**
  - `__init__(self, endpoint: _McpEndpoint, read_timeout_seconds: float | None = None, *, client_factory: _McpClientFactory = Client) -> None`.
  - Construtores de transporte aceitando `client_factory: _McpClientFactory = Client`:
    - `in_memory(cls, exchange_provider: ExchangeRateProvider | None = None, *, timeout_seconds: float = 8.0, client_factory: _McpClientFactory = Client) -> Self`
    - `stdio(cls, *, timeout_seconds: float, client_factory: _McpClientFactory = Client) -> Self` (preservando `args=["-m", "banco_agil.mcp.exchange_server", "--timeout", str(timeout_seconds)]` e `read_timeout_seconds=2 * timeout_seconds`)
    - `streamable_http(cls, *, url: str, timeout_seconds: float, client_factory: _McpClientFactory = Client) -> Self` (preservando `read_timeout_seconds=2 * timeout_seconds`)
  - `get_quote(self, currency: str = "USD") -> ExchangeQuote`:
    - Substituir entrada com **Dados Pessoais Sensíveis (PII)** detectados por `mask_pii(currency) != currency` por `""` antes do envio.
    - Abrir cliente via `async with self._client_factory(self._endpoint, read_timeout_seconds=self._read_timeout_seconds) as client:`.
    - Descobrir ferramenta via `await client.list_tools()`, exigindo `_QUOTE_TOOL`, chamar `await client.call_tool(_QUOTE_TOOL, {"moeda": currency})` e decodificar via `_decode_quote_result(result)`.
    - Propagar `DomainValidationError`; em qualquer outra exceção, logar apenas `type(exc).__name__` (sem `exc_info`) e levantar `ExchangeServiceError(_UNAVAILABLE_MESSAGE) from None`.
- **Fábrica `create_exchange_provider(settings: Settings, *, client_factory: _McpClientFactory = Client) -> ExchangeRateProvider`:**
  - Para `stdio`: `McpExchangeRateProvider.stdio(timeout_seconds=timeout_seconds, client_factory=client_factory)`.
  - Para `streamable_http`: `McpExchangeRateProvider.streamable_http(url=settings.resolve_mcp_exchange_url(), timeout_seconds=timeout_seconds, client_factory=client_factory)` (sem duplicar validação de URL).
  - Para `inmemory`: `McpExchangeRateProvider.in_memory(timeout_seconds=timeout_seconds, client_factory=client_factory)`.

### 3. Fachada Pública e Entrypoint CLI Fino (`src/banco_agil/mcp/__init__.py` e `src/banco_agil/mcp/exchange_server.py`)

- **`src/banco_agil/mcp/__init__.py`:**
  - Reexportar explicitamente apenas a superfície pública em `__all__`:
    - `McpExchangeRateProvider`
    - `create_exchange_mcp_server`
    - `create_exchange_provider`
  - Não exportar constantes ou helpers do codec de fio (`_QUOTE_TOOL`, `_INVALID_CURRENCY_PREFIX`, `_UNAVAILABLE_MESSAGE`, `_encode_*`, `_decode_*`).
- **`src/banco_agil/mcp/exchange_server.py`:**
  - Reduzir ao entrypoint CLI fino (`main()` + `if __name__ == "__main__":`), importando `create_exchange_mcp_server` de `banco_agil.mcp.exchange`.
  - Preservar integralmente o contrato operacional `python -m banco_agil.mcp.exchange_server` usado em `Makefile`, `docker-compose.yml` e `McpExchangeRateProvider.stdio`, incluindo validação de `--timeout` positivo/finito e `TransportSecuritySettings` com proteção contra DNS rebinding e `allowed_hosts`.

### 4. Remoção do Módulo Legado e Migração de Importadores

- Deletar `src/banco_agil/infrastructure/mcp_exchange_client.py` (sem shim de compatibilidade).
- Atualizar importadores internos para a fachada `banco_agil.mcp`:
  - `src/banco_agil/runtime.py`: `from banco_agil.mcp import create_exchange_provider`.
  - `src/banco_agil/evals/runner.py`: `from banco_agil.mcp import McpExchangeRateProvider`.
  - `tests/test_ui.py`: `from banco_agil.mcp import McpExchangeRateProvider`.
  - `tests/test_walking_skeleton.py`: atualizar `test_graph_has_no_mcp_exchange_dependency` para verificar ausência de `"banco_agil.mcp"`, `"mcp_exchange_client"` e `"McpExchangeRateProvider"` em `agents/graph.py`.
  - `tests/test_mcp_exchange.py`: migrar imports para `banco_agil.mcp` (mantendo `exchange_server` apenas para os testes da CLI), ajustar a verificação do logger do servidor para `"banco_agil.mcp.exchange"` e eliminar todos os monkeypatches de `Client` via `client_factory`.

## Fora de Escopo

- Alterações em `Makefile`, `docker-compose.yml`, `Dockerfile` ou contrato HTTP/stdio do servidor MCP.
- Alterações no comportamento de negócio de `ExchangeRateClient` (AwesomeAPI + fallback Frankfurter/BCE), `agents/tools.py` ou `agents/graph.py`.
- Refatoração dos helpers de privacidade/CSV em `evals/runner.py` (Spike — Candidato 6).
- Alterações em `GLOSSARY.md` ou criação/edição de ADRs (ADRs `005` e `009` permanecem integralmente preservados).
- Edição de `docs/backlog.md`, `docs/spec.md`, `README.md` e `AGENTS.md` dentro do worktree (atualizados no fechamento ao integrar na `main`).

## Seams de Teste Pré-Acordados (para execução com `/tdd`)

Seguindo *the interface is the test surface* e *replace, don't layer*:

| Seam | Interface sob teste | O que captura | O que fica fora (coberto por outro seam) |
| :--- | :--- | :--- | :--- |
| **1. Configuração de URL MCP** (`tests/test_mcp_exchange.py` / `tests/test_config_and_llm_factory.py`) | `Settings.resolve_mcp_exchange_url()` e `Settings(mcp_exchange_transport="streamable_http", ...)` | Fonte única da validação e mensagem de erro de `MCP_EXCHANGE_URL`, tanto na construção normal quanto quando a validação foi contornada via `Settings.model_construct` | Descoberta de ferramentas e chamadas MCP |
| **2. Fachada pública e isolamento do protocolo** (`tests/test_mcp_exchange.py`, `tests/test_walking_skeleton.py`) | `banco_agil.mcp.__all__`, `create_exchange_mcp_server`, `McpExchangeRateProvider.in_memory` | Ausência de vazamento das constantes/codec privados na fachada pública (`banco_agil.mcp`), remoção de `banco_agil.infrastructure.mcp_exchange_client`, ausência de acoplamento de `agents/graph.py` a `banco_agil.mcp`, round-trip completo de sucesso (AwesomeAPI + fallback Frankfurter), erro de validação sem PII e falha inesperada logando apenas a classe da exceção em `banco_agil.mcp.exchange` | Execução da CLI (`exchange_server.main`) e wiring do `ConversationRuntime` |
| **3. Costura de transporte e falhas de protocolo** (`tests/test_mcp_exchange.py`) | `create_exchange_provider(settings, client_factory=...)` e `McpExchangeRateProvider(..., client_factory=...)` | Seleção correta de endpoint e `read_timeout_seconds` para `inmemory`/`stdio`/`streamable_http`, resposta em JSON via `TextContent`, e falhas controladas (`session`, ferramenta ausente, `bad_json`, `bad_shape`, `no_quote`) **sem nenhum `monkeypatch` em `mcp.client.Client`** | Chamadas reais de rede ou subprocesso (bloqueadas por `tests/conftest.py`) |
| **4. Entrypoint CLI fino** (`tests/test_mcp_exchange.py`) | `banco_agil.mcp.exchange_server.main()` | Leitura de `EXCHANGE_TIMEOUT_SECONDS`, transporte `stdio`, `streamable-http` com `stateless_http=True`, `json_response=True` e `TransportSecuritySettings` (DNS rebinding + `allowed_hosts`), rejeição de `--timeout` inválido | Codec interno de `CallToolResult` (coberto nos seams 2 e 3) |

### Estratégia *Replace, Don't Layer* nos Testes Existentes

- **`tests/test_mcp_exchange.py`**:
  - Substituir o import de `banco_agil.infrastructure.mcp_exchange_client` por `from banco_agil.mcp import McpExchangeRateProvider, create_exchange_mcp_server, create_exchange_provider`.
  - Remover todos os 5 `monkeypatch.setattr` sobre `mcp_exchange_client.Client`, `Client.list_tools` e `Client.call_tool`, substituindo-os por injeção via `client_factory` (um spy envolvendo `Client(server)` para os 3 transportes e um fake async-context-manager mínimo para os cenários de erro/JSON textual).
  - Adicionar asserção sobre a fachada pública `banco_agil.mcp.__all__` garantindo que constantes/helpers de fio não vazam e que o módulo legado `banco_agil.infrastructure.mcp_exchange_client` não existe mais.
  - Atualizar a verificação de log do servidor (`r.name == "banco_agil.mcp.exchange"`).
- **`tests/test_ui.py`** e **`tests/test_walking_skeleton.py`**:
  - Atualizar imports/asserções de módulo no lugar existente, sem criar arquivos de teste redundantes.

## Sequência de Fatias Verticais Sugerida (`/tdd`: red → green por fatia)

1. **Fatia 1 — Resolução única de `MCP_EXCHANGE_URL` (`src/banco_agil/config.py`):**
   - Red → green para `Settings.resolve_mcp_exchange_url()` (incluindo `Settings.model_construct` com `None` e string em branco) → implementar em `src/banco_agil/config.py` e delegar a partir de `require_mcp_exchange_url`.
2. **Fatia 2 — Módulo profundo `src/banco_agil/mcp/exchange.py` + fachada `src/banco_agil/mcp/__init__.py`:**
   - Red → green nos testes de fachada pública, protocolo em memória (sucesso, fallback, validação sem PII, erro inesperado logado em `banco_agil.mcp.exchange`), `client_factory` para transportes (`inmemory`/`stdio`/`streamable_http`) e falhas de protocolo/JSON textual sem monkeypatch em `Client` → implementar `src/banco_agil/mcp/exchange.py` e `src/banco_agil/mcp/__init__.py`.
3. **Fatia 3 — Entrypoint CLI fino (`src/banco_agil/mcp/exchange_server.py`):**
   - Manter testes de CLI verdes em `tests/test_mcp_exchange.py` → reduzir `src/banco_agil/mcp/exchange_server.py` ao entrypoint fino delegando para `create_exchange_mcp_server`.
4. **Fatia 4 — Remoção de `infrastructure/mcp_exchange_client.py` e migração dos importadores:**
   - Remover `src/banco_agil/infrastructure/mcp_exchange_client.py`, migrar `src/banco_agil/runtime.py`, `src/banco_agil/evals/runner.py`, `tests/test_ui.py` e `tests/test_walking_skeleton.py`.
5. **Gate final no worktree:** rodar `make check` e `make evals` (100% verdes).

## Critérios de Aceite

- [ ] `src/banco_agil/mcp/exchange.py` colocaliza constantes privadas, codec pareado de erro/resposta, `create_exchange_mcp_server`, `McpExchangeRateProvider` e `create_exchange_provider` atrás de `ExchangeRateProvider`.
- [ ] `src/banco_agil/mcp/__init__.py` expõe apenas a fachada pública (`McpExchangeRateProvider`, `create_exchange_mcp_server`, `create_exchange_provider`), mantendo constantes de ferramenta/erro e codec estritamente privados.
- [ ] `src/banco_agil/mcp/exchange_server.py` atua apenas como entrypoint CLI fino, preservando `python -m banco_agil.mcp.exchange_server`, `Makefile`, `docker-compose.yml`, `stdio` e a proteção contra DNS rebinding do ADR 009.
- [ ] `src/banco_agil/infrastructure/mcp_exchange_client.py` foi removido sem shim, e `runtime.py`, `evals/runner.py` e os testes importam de `banco_agil.mcp`.
- [ ] `Settings.resolve_mcp_exchange_url()` centraliza a validação de `MCP_EXCHANGE_URL` em `src/banco_agil/config.py`, eliminando a validação duplicada em `create_exchange_provider` sem usar `cast` nem `assert`.
- [ ] `McpExchangeRateProvider` e `create_exchange_provider` aceitam `client_factory` opcional (padrão `mcp.client.Client`), e `tests/test_mcp_exchange.py` tem **zero** `monkeypatch` em `Client` ou seus métodos.
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`, cobertura geral mantida ≥ 98%).
- [ ] `make evals` 100% verde (12/12 cenários, 0 vazamentos de PII).
- [ ] Commits pequenos em Conventional Commits na branch `feat/f15-mcp-exchange-colocation`, sem editar `docs/backlog.md`, `docs/spec.md`, `GLOSSARY.md`, `README.md`, `AGENTS.md` nem commitar arquivos de tooling local.

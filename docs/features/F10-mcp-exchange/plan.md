# Plano — F10: Servidor MCP de Câmbio e Fronteira de Segurança

## Objetivo
Isolar a consulta de câmbio em um **servidor MCP stateless e sem PII** construído sobre o SDK oficial `mcp` v2 (`MCPServer`, sucessor do `FastMCP` na v2.3.0) e consumi-lo no Agente de Câmbio via **`langchain-mcp-adapters`** (`MultiServerMCPClient` e `load_mcp_tools`), preservando todas as ferramentas autenticadas/PII localmente no grafo com `ToolRuntime` e documentando a fronteira de segurança no **ADR 009**.

## Dependências (`pyproject.toml`)
- Declarar explicitamente em `[project].dependencies`:
  - `mcp>=2.0.0,<3` *(já presente transitivamente na versão `2.3.0`; no `mcp` v2, a classe `FastMCP` foi renomeada para `MCPServer` em `mcp.server` / `mcp.server.mcpserver` — `mcp.server.fastmcp` não existe mais)*
  - `langchain-mcp-adapters>=0.3.0` *(verificada compatível com `mcp==2.3.0`)*
- Rodar `uv sync` no worktree para atualizar `uv.lock`.

## Escopo

### 1. Servidor MCP Stateless de Câmbio (`src/banco_agil/mcp/exchange_server.py`)
- **Zero estado de sessão e zero PII:** o servidor não recebe CPF, data de nascimento nem `thread_id`, e não importa nem acessa repositórios CSV ou `CreditService`.
- **Factory `create_exchange_mcp_server(exchange_provider: ExchangeRateProvider | None = None, *, timeout_seconds: float = 8.0) -> MCPServer`:**
  - Instancia `MCPServer("banco-agil-cambio")` (de `mcp.server`).
  - Quando `exchange_provider is None`, instancia `ExchangeRateClient(timeout_seconds=timeout_seconds)`.
  - Registra a ferramenta `@server.tool(name="consultar_cotacao_cambio")` (assíncrona):
    - Assinatura pública mínima: `async def consultar_cotacao_cambio(moeda: str = "USD") -> ExchangeQuote` (ou retorno estruturado serializável equivalente validável como `ExchangeQuote`).
    - Chama `await provider.get_quote(moeda)` (preservando a normalização de moeda e o fallback automático AwesomeAPI → Frankfurter/BCE).
    - **Tratamento controlado de erros MCP (`ToolError` de `mcp.server.mcpserver.exceptions`):**
      - Em `DomainValidationError as exc`: levanta `ToolError(f"INVALID_CURRENCY: {exc}")` (mensagem de domínio limpa, sem ecoar entrada bruta).
      - Em `ExchangeServiceError` ou `Exception` inesperada: registra apenas `logger.error("Falha no servidor MCP de câmbio (%s)", type(exc).__name__)` (em stderr via `logging`, nunca `print` em stdout!) e levanta `ToolError("EXCHANGE_UNAVAILABLE: Não foi possível consultar a cotação no momento.")`.
- **Entrypoint CLI (`python -m banco_agil.mcp.exchange_server`):**
  - Configura `configure_logging()` (garantindo logs em stderr com filtro LGPD, jamais corrompendo o stream `stdio`).
  - Aceita `--transport {stdio,streamable-http}` (padrão `stdio`), `--host` (padrão `127.0.0.1`), `--port` (padrão `8001`), `--timeout` (lendo default de `Settings().exchange_timeout_seconds`) e `--allowed-host` (repetível, para allowlist de `Host` no Streamable HTTP).
  - Para `streamable-http`: passa opções de transporte **no método `server.run(...)`** (nunca no construtor `MCPServer(...)`, conforme API do `mcp` v2.3.0), com `stateless_http=True`, `json_response=True` e `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[...])` (incluindo `127.0.0.1:*`, `localhost:*`, `[::1]:*` e os hosts extras informados via `--allowed-host`, evitando `421 Misdirected Request` no Docker Compose sem desabilitar a proteção contra DNS rebinding!).
  - **`run()` estritamente sob `if __name__ == "__main__":`** (import sem efeitos colaterais).

### 2. Cliente MCP via `langchain-mcp-adapters` (`src/banco_agil/infrastructure/mcp_exchange_client.py`)
- Implementar `McpExchangeRateProvider` cumprindo integralmente o `Protocol` de domínio **`ExchangeRateProvider`** (`async def get_quote(self, currency: str = "USD") -> ExchangeQuote`):
  - **Por que adaptar o protocolo MCP atrás de `ExchangeRateProvider`:**
    1. O Agente de Câmbio (`consultar_cotacao` em `agents/tools.py`) **mantém** o gate de autenticação em código via `ToolRuntime` (`has_authenticated_client(runtime.state)`), a defesa em profundidade em 2 camadas (roteador + ferramenta) e a formatação determinística em português (incluindo data de referência do BCE).
    2. Toda consulta de câmbio executa o **protocolo MCP real via `langchain-mcp-adapters`**, descobrindo e invocando a ferramenta remota `consultar_cotacao_cambio` e revalidando a resposta no modelo de domínio imutável `ExchangeQuote`!
  - **Três modos de transporte MCP suportados:**
    1. **`inmemory` (padrão local — zero subprocesso, zero socket, protocolo MCP real):** conecta a uma instância `MCPServer` via `mcp.client.Client(server)` e carrega a ferramenta LangChain via `await load_mcp_tools(client.session, handle_tool_errors=False)` de `langchain_mcp_adapters.tools`.
    2. **`stdio` (subprocesso local):** usa `MultiServerMCPClient({"exchange": {"transport": "stdio", "command": sys.executable, "args": ["-m", "banco_agil.mcp.exchange_server", "--transport", "stdio"]}})` e `async with client.session("exchange") as session: tools = await load_mcp_tools(session, handle_tool_errors=False)`.
    3. **`streamable_http` (serviço HTTP MCP dedicado — usado no Docker Compose):** usa `MultiServerMCPClient({"exchange": {"transport": "streamable_http", "url": url, "timeout": timeout_seconds}})` e `async with client.session("exchange") as session: tools = await load_mcp_tools(session, handle_tool_errors=False)`.
  - **Conversão de resultado e erros:**
    - Valida localmente a moeda com `normalize_currency_code(currency)`? **Não** antes de chamar o servidor — ou deixar o servidor validar para exercitar o fluxo de erro MCP (`INVALID_CURRENCY:` → `DomainValidationError`)! (Validar no servidor comprova o tratamento de erro `isError=True` / `ToolException` ponta a ponta; se a entrada não for string/estiver vazia, o servidor devolve `INVALID_CURRENCY:`.)
    - Localiza a ferramenta `consultar_cotacao_cambio` retornada por `load_mcp_tools`, executa `await mcp_tool.ainvoke({"moeda": currency})`, interpreta a resposta (dict, JSON em string ou lista de blocos de texto `[{"type": "text", "text": "..."}]` retornada por `langchain-mcp-adapters`) e valida com `ExchangeQuote.model_validate(...)`.
    - Em `ToolException as exc`: se a mensagem iniciar com `"INVALID_CURRENCY:"`, extrai a orientação limpa e levanta `DomainValidationError(...) from None`; caso contrário registra `logger.error("Falha na chamada MCP de câmbio (%s)", type(exc).__name__)` e levanta `ExchangeServiceError("Não foi possível consultar a cotação no momento.") from None`.
    - Em erro de transporte/sessão/JSON inválido/ferramenta ausente: registra apenas `type(exc).__name__` e levanta `ExchangeServiceError("Não foi possível consultar a cotação no momento.") from None`.
- **Factory de provedor (`create_exchange_provider(settings: Settings) -> ExchangeRateProvider`):**
  - Lê `settings.mcp_exchange_transport` (`"inmemory"` | `"stdio"` | `"streamable_http"`):
    - `"inmemory"` (padrão): cria `McpExchangeRateProvider.in_memory(timeout_seconds=settings.exchange_timeout_seconds)`.
    - `"stdio"`: cria `McpExchangeRateProvider.stdio(timeout_seconds=settings.exchange_timeout_seconds)`.
    - `"streamable_http"`: exige `settings.mcp_exchange_url` preenchido e cria `McpExchangeRateProvider.streamable_http(url=settings.mcp_exchange_url, timeout_seconds=settings.exchange_timeout_seconds)`.

### 3. Configuração, Integração no Grafo/API/UI/Docker e Makefile
- **`src/banco_agil/config.py` e `.env.example`:**
  - Adicionar em `Settings`:
    - `mcp_exchange_transport: Literal["inmemory", "stdio", "streamable_http"] = "inmemory"`
    - `mcp_exchange_url: str | None = None` *(reaproveitando validação HTTP/HTTPS de `api_base_url`)*
    - `@model_validator(mode="after")`: exigir `mcp_exchange_url` preenchido quando `mcp_exchange_transport == "streamable_http"`.
  - Documentar `MCP_EXCHANGE_TRANSPORT` e `MCP_EXCHANGE_URL` (comentados/opcionais) em `.env.example`.
- **`src/banco_agil/agents/graph.py`, `src/banco_agil/api/app.py`, `src/banco_agil/ui/app.py`:**
  - `api/app.py` (no `lifespan`) e `ui/app.py` (no modo direto em memória) passam a usar `create_exchange_provider(settings)` (em vez de instanciar `ExchangeRateClient` diretamente), fazendo com que o Agente de Câmbio consuma o servidor MCP via `langchain-mcp-adapters` em ambos os entrypoints!
  - Preservar `build_graph(..., exchange_provider=...)` aceitando qualquer `ExchangeRateProvider` (compatibilidade total com todos os testes existentes e evals!).
- **`Makefile`:**
  - Adicionar alvo `mcp-exchange`: `uv run python -m banco_agil.mcp.exchange_server --transport streamable-http --host 127.0.0.1 --port 8001` e incluir `mcp-exchange` em `.PHONY`.
- **`docker-compose.yml`:**
  - Adicionar serviço `mcp-exchange`:
    - Reutiliza a mesma imagem `banco-agil:local` (`build: .`).
    - **Sem volume `./data` e sem chaves de LLM/Langfuse** (isolamento físico de PII/credenciais no contêiner!).
    - `command: ["python", "-m", "banco_agil.mcp.exchange_server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8001", "--allowed-host", "mcp-exchange", "--allowed-host", "mcp-exchange:8001"]`.
    - `healthcheck` via socket TCP local em Python (`socket.create_connection(("127.0.0.1", 8001), timeout=3)`).
  - Atualizar serviço `api`:
    - `depends_on: { mcp-exchange: { condition: service_healthy } }`
    - `environment`: `MCP_EXCHANGE_TRANSPORT: streamable_http` e `MCP_EXCHANGE_URL: http://mcp-exchange:8001/mcp`.

### 4. Documentação Arquitetural (`docs/adr/009-servidor-mcp-cambio-e-fronteira-de-seguranca.md`)
Seguindo `docs/adr/000-template.md`, registrar:
- **Decisão e fronteira de segurança (MCP vs. `ToolRuntime` / `InjectedState`):**
  - Tabela explícita separando:
    - **Exposto via MCP (stateless, sem PII):** `consultar_cotacao_cambio` (AwesomeAPI + fallback Frankfurter/BCE), rodando sobre `MCPServer` (`mcp` v2, evolução do `FastMCP`) e consumido via `langchain-mcp-adapters` (`MultiServerMCPClient` / `load_mcp_tools`) atrás do contrato `ExchangeRateProvider`.
    - **Mantido local no grafo com `ToolRuntime`:** `autenticar_cliente`, `consultar_limite`, `solicitar_aumento_limite` (HITL `interrupt()`), `calcular_e_atualizar_score`, handoffs e `encerrar_atendimento`, além do gate de autenticação da ferramenta `consultar_cotacao`.
  - Por que ferramentas autenticadas **não** são movidas para MCP no MVP: evitaria injeção confiável de estado (`ToolRuntime`), forçaria propagação de identidade/CPF pelo protocolo MCP, complicaria `interrupt()` HITL distribuído e ampliaria a superfície de exposição de PII.
  - Por que o `mcp-exchange` no `docker-compose.yml` roda sem acesso ao volume `./data` e com `TransportSecuritySettings` (proteção contra DNS rebinding ativa com allowlist explícita).
- **Alternativas descartadas:**
  - Expor ferramentas de crédito/autenticação via MCP sem camada de OAuth2/JWT + token exchange (quebraria o princípio de menor privilégio e o gate de autenticação).
  - Ligar a ferramenta MCP crua direto no `cambio_tools` sem o wrapper de gate (`ToolRuntime`) — perderia a defesa em profundidade da camada de ferramenta e acoplaria a formatação de resposta ao formato bruto do servidor.
  - Usar transporte SSE legado (descontinuado em favor de `streamable-http` no protocolo MCP atual).
- **Consequências, trade-offs e Evolução e Escala:**
  - Quando novos domínios ou equipes precisarem de ferramentas autenticadas via MCP, introduzir **MCP Auth (OAuth 2.1 / JWT com scopes granulares)** + propagação de identidade verificada (sem aceitar CPF livre do modelo), pool persistente de sessões/conexões HTTP, cache distribuído com TTL (30–60s) e circuit breaker no serviço `mcp-exchange` (alinhado ao ADR 005).

## Fora de Escopo
- Migrar ferramentas de triagem, crédito ou entrevista para MCP (explicitamente vedado pela fronteira de segurança no MVP).
- OAuth 2.1 / mTLS entre `api` e `mcp-exchange` (documentado em *Evolução e Escala* no ADR 009).
- README final consolidado (`F11`).
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes (100% determinísticos, zero rede, zero subprocessos)
- `tests/test_mcp_exchange.py`:
  1. **Servidor MCP (`create_exchange_mcp_server`):**
     - Verifica registro da ferramenta `consultar_cotacao_cambio` (schema com `moeda` default `"USD"`, sem campos de PII/CPF/auth).
     - Testa chamada real via `mcp.client.Client(server)` em memória com `ExchangeRateClient(transport=httpx.MockTransport(...))`:
       - Sucesso na AwesomeAPI → retorna `ExchangeQuote` estruturado.
       - Fallback automático Frankfurter/BCE → preserva `source="Frankfurter (BCE)"`, `is_reference_rate=True` e `timestamp="2026-11-20"`.
       - Moeda inválida (`BRL` / desconhecida) → retorna erro MCP controlado (`INVALID_CURRENCY:`) sem ecoar PII.
       - Falha total / exceção inesperada → retorna erro MCP controlado (`EXCHANGE_UNAVAILABLE:`) e loga apenas `type(exc).__name__`.
  2. **Cliente `McpExchangeRateProvider` com `langchain-mcp-adapters` real (modo `inmemory`):**
     - Usa `mcp.client.Client(server)` + `load_mcp_tools(client.session, handle_tool_errors=False)` reais em memória!
     - Valida retorno de `ExchangeQuote` (AwesomeAPI e fallback Frankfurter), tradução de `INVALID_CURRENCY:` → `DomainValidationError`, e tradução de `EXCHANGE_UNAVAILABLE:` / falha de sessão / payload corrompido / ferramenta ausente → `ExchangeServiceError`.
  3. **Modos `stdio` e `streamable_http` + `create_exchange_provider(settings)`:**
     - Testa validação de `Settings` (`mcp_exchange_transport` e exigência de `mcp_exchange_url` em `streamable_http`).
     - Testa que `McpExchangeRateProvider.stdio(...)` e `McpExchangeRateProvider.streamable_http(...)` montam corretamente a configuração do `MultiServerMCPClient` e carregam via `load_mcp_tools` (com `MultiServerMCPClient` substituído por fake/mock no teste — zero sockets e zero subprocessos!).
  4. **Integração ponta a ponta no Agente de Câmbio (`build_graph` + `McpExchangeRateProvider` in-memory):**
     - Fluxo autenticado completo: `triagem` (autentica) → `cambio` → `consultar_cotacao` → `McpExchangeRateProvider` → `langchain-mcp-adapters` → `MCPServer` → `ExchangeRateClient(MockTransport)` (tanto AwesomeAPI quanto fallback Frankfurter preservando data `2026-11-20` e redigindo CPF/nascimento!).
     - Confirma gate de autenticação preservado (chamada sem autenticação bloqueada antes de acionar o MCP).
  5. **CLI `exchange_server.main`:**
     - Testa parsing de argumentos (`stdio` e `streamable-http` com `--host`, `--port`, `--allowed-host`) com `server.run` mockado (verificando `TransportSecuritySettings` com allowlist correta e `stateless_http=True`, `json_response=True`).
- Verificar que `tests/test_ui.py` e `tests/test_api.py` continuam 100% verdes (atualizando eventual monkeypatch se `ui/app.py` ou `api/app.py` passar a chamar `create_exchange_provider` em vez de `ExchangeRateClient` diretamente!).

## Critérios de Aceite
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [ ] `make evals` 100% verde (12/12 cenários, 0 vazamentos de PII).
- [ ] `docker compose config --quiet` e `docker build` válidos.
- [ ] Cobertura ≥ 95% nos módulos novos (`mcp/exchange_server.py` e `infrastructure/mcp_exchange_client.py`) e cobertura geral mantida ≥ 98%.
- [ ] `docs/adr/009-servidor-mcp-cambio-e-fronteira-de-seguranca.md` criado seguindo `docs/adr/000-template.md`.
- [ ] Commits pequenos em Conventional Commits na branch `feat/f10-mcp-exchange` (incluindo `plan.md`).

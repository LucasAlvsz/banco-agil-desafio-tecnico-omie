# Plano — F06: Agente de Câmbio (AwesomeAPI + Fallback Frankfurter)

## Objetivo
Implementar o **Agente de Câmbio** completo e o cliente HTTP assíncrono de cotações (`AwesomeAPI` como fonte primária e `Frankfurter API / BCE` como fallback automático), desacoplados por um `Protocol` de domínio (`ExchangeRateProvider`) preparado para consumo direto na UI, em FastAPI (F09) e em servidor MCP (F10).

## Dependências (`pyproject.toml`)
- Declarar explicitamente `httpx>=0.28.0` em `[project].dependencies` (já presente transitivamente, agora importado diretamente).
- Adicionar `pytest-asyncio>=0.25.0` em `[dependency-groups].dev` e `asyncio_mode = "auto"` em `[tool.pytest.ini_options]`.

## Escopo

### 1. Contratos de Domínio (`src/banco_agil/domain/`)
- `errors.py`: adicionar `ExchangeServiceError(DomainError)` para indisponibilidade controlada dos provedores de câmbio.
- `models.py`: adicionar `ExchangeQuote(DomainModel)` (imutável, sem `NaN`/`inf`):
  - `currency_code: str` (`^[A-Z]{3}$`)
  - `currency_name: str` (`min_length=1`)
  - `target_currency: str = "BRL"`
  - `bid: float` (`gt=0`)
  - `ask: float | None = Field(default=None, gt=0)`
  - `timestamp: str` (`min_length=1`)
  - `source: str` (`min_length=1`)
  - `is_reference_rate: bool = False` *(indica taxa de referência diária do fallback Frankfurter/BCE)*
- `rules.py`: adicionar `normalize_currency_code(value: object) -> str`:
  - Normaliza espaços, caixa e acentos (reaproveitando `_text`).
  - Mapeia nomes comuns em português e símbolos (`dólar`/`dolar`/`dolar americano`/`us$` → `USD`, `euro` → `EUR`, `libra`/`libra esterlina` → `GBP`, `iene` → `JPY`, `franco`/`franco suíço` → `CHF`, `peso argentino` → `ARS`, `dólar canadense` → `CAD`, `dólar australiano` → `AUD`, `yuan`/`renminbi` → `CNY`, `bitcoin` → `BTC`).
  - Aceita códigos ISO ASCII de 3 letras (`[A-Z]{3}`).
  - Rejeita `BRL` / `real` / `reais` com `DomainValidationError` amigável explicando que o Real já é a moeda de referência.
  - Rejeita entradas vazias, não-string ou desconhecidas com `DomainValidationError` (sem ecoar entrada potencialmente sensível).
- `repositories.py`: adicionar `ExchangeRateProvider(Protocol)` com `async def get_quote(self, currency: str = "USD") -> ExchangeQuote: ...`.

### 2. Cliente HTTP Assíncrono (`src/banco_agil/infrastructure/exchange_client.py` e `config.py`)
- Adicionar em `Settings` (`config.py`): `exchange_timeout_seconds: float = Field(default=8.0, gt=0.0)` (documentar em `.env.example`).
- Criar `ExchangeRateClient`:
  - Construtor aceitando `timeout_seconds: float = 8.0` e `transport: httpx.AsyncBaseTransport | None = None` (permitindo testes determinísticos com `httpx.MockTransport` sem tocar sockets reais).
  - `async def get_quote(self, currency: str = "USD") -> ExchangeQuote`:
    1. Valida e normaliza a moeda com `normalize_currency_code(currency)` (lança `DomainValidationError` antes de qualquer chamada HTTP).
    2. Tenta **AwesomeAPI** (`https://economia.awesomeapi.com.br/json/last/{code}-BRL`): valida status HTTP, JSON dict, chave `{code}BRL`, `bid` finito `> 0` (e `ask` se presente), monta `ExchangeQuote(..., source="AwesomeAPI", is_reference_rate=False)`.
    3. Em qualquer falha da AwesomeAPI (timeout, erro de rede, status 4xx/5xx, JSON inválido, par ausente): registra `logger.warning("AwesomeAPI indisponível para %s (%s)", code, type(exc).__name__)` e aciona **Frankfurter API** (`https://api.frankfurter.dev/v1/latest`, params `{"base": code, "symbols": "BRL"}`), validando `rates["BRL"]` finito `> 0` e `date`, montando `ExchangeQuote(..., source="Frankfurter (BCE)", is_reference_rate=True)`.
    4. Se ambos falharem: registra `logger.error("Falha na consulta de câmbio para %s (%s)", code, type(exc).__name__)` e levanta `ExchangeServiceError("Não foi possível consultar a cotação no momento.") from None`.

### 3. Agente de Câmbio (`src/banco_agil/agents/`)
- `prompts.py`: substituir `build_exchange_placeholder_prompt` por `build_exchange_prompt(state)`:
  - Persona única contínua (sem anunciar transferências).
  - Orienta a usar `consultar_cotacao` (padrão `"USD"` quando o cliente pedir dólar ou não indicar outra moeda).
  - Apresenta a cotação claramente em Reais (`BRL`); quando for taxa de referência diária (fallback Frankfurter/BCE), informa explicitamente a data de referência ao cliente.
  - Encerra o tópico de cotação com mensagem amigável perguntando se pode ajudar em outro assunto; usa `voltar_para_triagem` quando o cliente mudar de assunto ou `encerrar_atendimento` se desejar encerrar.
- `tools.py`: criar `build_exchange_tools(exchange_provider: ExchangeRateProvider)` retornando:
  - `consultar_cotacao(moeda: str = "USD", runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
    - Gate em código via `has_authenticated_client(runtime.state)` (se falso, retorna `active_agent="triagem"` sem chamar o provedor).
    - Executa `exchange_provider.get_quote(moeda)` por meio de ponte assíncrona segura (usando `asyncio.run(...)` fora de event loop e executando em thread dedicada caso já exista event loop ativo).
    - Em `DomainValidationError`: retorna `ToolMessage` com a orientação limpa de domínio (mantendo `active_agent="cambio"`).
    - Em `ExchangeServiceError` (ou erro inesperado do provedor): loga apenas `type(exc).__name__` e retorna `ToolMessage` controlada informando indisponibilidade temporária (sem derrubar a conversa).
    - Em sucesso: retorna `ToolMessage` formatada com moeda, valor de compra (`bid`), venda (`ask`, se disponível), fonte e timestamp/data de referência (deixando explícito quando `is_reference_rate=True`).
- `graph.py`:
  - Estender `build_graph(model, credit_service, exchange_provider: ExchangeRateProvider | None = None, checkpointer=None)` — usando `ExchangeRateClient()` por padrão quando `exchange_provider is None` para preservar compatibilidade.
  - Conectar o nó `cambio` a `exchange_tools = [*build_exchange_tools(provider), *specialist_tools]` e `build_exchange_prompt`.
- `ui/app.py`: instanciar `ExchangeRateClient(timeout_seconds=settings.exchange_timeout_seconds)` e injetar em `build_graph(...)`.

### 4. Documentação Arquitetural (`docs/adr/005-integracao-cambio-com-fallback.md`)
Seguindo `docs/adr/000-template.md`, registrar:
- Escolha de APIs estruturadas (**AwesomeAPI** primária + **Frankfurter/BCE** fallback) via `httpx.AsyncClient` atrás de `ExchangeRateProvider`.
- Alternativas descartadas: **Tavily / SerpAPI** (busca web exige chave adicional e extração por LLM sobre texto livre, elevando risco de alucinação de números financeiros) e provedor único sem fallback.
- Evolução e Escala: cache distribuído com TTL curto (30–60s), circuit breaker entre provedores, provedor contratado com SLA em produção e exposição do cliente via servidor MCP (`F10`).

## Fora de Escopo
- Servidor `FastMCP` e integração via `langchain-mcp-adapters` (`F10`).
- Filtro global de mascaramento LGPD em logs (`F07`), telemetria/Langfuse (`F08`), FastAPI (`F09`).
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes (100% determinísticos, zero rede)
- `tests/test_exchange_client.py` (usando `httpx.MockTransport` — zero sockets reais):
  1. `normalize_currency_code`: nomes em PT com/sem acento, símbolos (`US$`), ISO 3 letras, rejeição de `BRL`/`real`, entradas inválidas/vazias/não-string sem ecoar entrada.
  2. Sucesso na AwesomeAPI (com `bid`, `ask`, `create_date`, `is_reference_rate=False`).
  3. Fallback automático para Frankfurter em timeout, erro de rede, HTTP 500/404 ou JSON malformado da AwesomeAPI (`source="Frankfurter (BCE)"`, `is_reference_rate=True`, `timestamp=date`).
  4. Falha total (ambos provedores falham) → levanta `ExchangeServiceError` sem cadeia de exceção bruta e registra warning/error apenas com código e `type(exc).__name__`.
  5. Validação de resposta inválida (taxa `<= 0`, `NaN`, chave ausente) dispara fallback / `ExchangeServiceError`.
- `tests/test_exchange_agent.py` (usando `ExchangeRateProvider` fake + `ScriptedChatModel`):
  1. Schema seguro (`consultar_cotacao` expõe apenas `moeda` com default `"USD"`, sem `runtime`/`cpf`) e gate pré-autenticação (recusa e volta para `triagem` sem consultar provedor).
  2. Fluxo completo `triagem` (autentica) → `cambio` → `consultar_cotacao("dólar")` (AwesomeAPI) → apresenta cotação amigável → `voltar_para_triagem` preservando autenticação ou `encerrar_atendimento`.
  3. Fluxo de fallback Frankfurter: `ToolMessage` indica explicitamente taxa de referência e data (`2026-11-20`), e a **data de referência é preservada intacta** na resposta final do `TurnResult` (não censurada por `_redact_reply`).
  4. Moeda inválida (`BRL` / desconhecida) e indisponibilidade (`ExchangeServiceError`) tratadas de forma controlada sem derrubar a conversa.
  5. Execução de `consultar_cotacao` dentro de um event loop assíncrono já ativo (`async def test_...`) validando a ponte síncrona→assíncrona sem `RuntimeError`.
  6. Isolamento físico de `cambio_tools` (`consultar_cotacao`, `voltar_para_triagem`, `encerrar_atendimento`).

## Critérios de Aceite
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [ ] Cobertura ≥ 95% nos módulos novos (`infrastructure/exchange_client.py` e `agents/`).
- [ ] `docs/adr/005-integracao-cambio-com-fallback.md` criado e revisado.
- [ ] Commits pequenos em Conventional Commits na branch `feat/f06-cambio` (incluindo `plan.md`).

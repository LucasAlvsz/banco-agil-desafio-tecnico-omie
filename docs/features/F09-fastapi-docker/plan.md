# Plano — F09: FastAPI, UI Híbrida e Docker

## Objetivo
Expor o atendimento multiagente do Banco Ágil via **API REST assíncrona em FastAPI** (`/health`, `/api/v1/chat`), evoluir a **interface Streamlit para modo híbrido** (execução direta em memória ou modo cliente HTTP via `API_BASE_URL`) e empacotar ambos os serviços com **Docker** (`Dockerfile` multi-stage, `.dockerignore` e `docker-compose.yml`).

## Dependências (`pyproject.toml`)
- Adicionar em `[project].dependencies`:
  - `fastapi>=0.115.0`
  - `uvicorn>=0.34.0`
- Executar `uv sync` no worktree para atualizar `uv.lock`.

## Escopo

### 1. Configuração e Concorrência de Observabilidade (`config.py`, `.env.example`, `infrastructure/observability.py`)
- **`src/banco_agil/config.py` e `.env.example`:**
  - Adicionar em `Settings`:
    - `api_base_url: str | None = None` *(vazio/None = modo direto em memória; URL `http://...` ou `https://...` = modo cliente HTTP na UI; normalizar removendo `/` final e rejeitar esquema diferente de `http`/`https` ou espaços inválidos).*
    - `api_timeout_seconds: float = Field(default=60.0, gt=0.0)` *(timeout HTTP da UI ao consultar a API).*
  - Documentar `API_BASE_URL` e `API_TIMEOUT_SECONDS` (comentados/opcionais) em `.env.example`.
- **`src/banco_agil/infrastructure/observability.py`:**
  - Atualizar `LangfuseObservability` para suportar criação de um `CallbackHandler` por requisição/turno quando inicializado pela factory (evitando estado compartilhado entre requisições HTTP concorrentes na API), mantendo compatibilidade total com `LangfuseObservability(client, handler)` nos testes existentes.

### 2. API REST Assíncrona em FastAPI (`src/banco_agil/api/`)
- **`src/banco_agil/api/schemas.py`:**
  - `HealthResponse(BaseModel)` (`frozen=True`):
    - `status: Literal["ok"] = "ok"`
    - `service: str = "banco-agil"`
    - `langfuse_enabled: bool`
  - `ChatRequest(BaseModel)` (`frozen=True`, `extra="forbid"`):
    - `thread_id: str | None = Field(default=None, min_length=1, max_length=128)` *(quando omitido, a API gera um UUID v4 para nova conversa; rejeitar `thread_id` composto só por espaços).*
    - `message: str | None = None`
    - `approve: StrictBool | None = None` *(uso de `StrictBool` preservando a garantia HITL do ADR 004: valores como `1` ou `"true"` não são coagidos).*
    - `@model_validator(mode="after")`: exigir exatamente um dos campos `message` ou `approve`.
  - `ChatResponse(BaseModel)` (`frozen=True`):
    - `thread_id: str`
    - `reply: str`
    - `conversation_ended: bool`
    - `active_agent: AgentName`
    - `authenticated: bool`
    - `pending_confirmation: LimitIncreaseConfirmation | None = None`
    - `telemetry: TurnTelemetry`
    - `langfuse_enabled: bool`
- **`src/banco_agil/api/app.py`:**
  - Container de runtime tipado em `app.state.runtime` guardando `graph`, `settings`, `observability`, e controle de concorrência por `thread_id` (`dict[str, asyncio.Lock]` protegido por lock de criação) para serializar turnos concorrentes da mesma sessão.
  - Factory `create_app(*, settings: Settings | None = None, graph: ConversationGraph | None = None, observability: LangfuseObservability | None = None) -> FastAPI`:
    - Usa `@asynccontextmanager async def lifespan(app: FastAPI)` (padrão oficial FastAPI):
      - **Startup:** chama `configure_logging()`; quando `graph` não foi injetado, instancia `Settings()`, `CreditService` (CSV), `ExchangeRateClient`, `create_langfuse_observability(settings)` e `build_graph(create_chat_model(settings), ...)`.
      - **Shutdown:** chama `observability.flush()` quando ativo e libera referências do runtime.
    - **Import seguro:** exportar `app = create_app()` no módulo sem executar I/O de rede/arquivos na importação (toda inicialização ocorre exclusivamente no `lifespan`).
  - **Endpoints:**
    - `GET /health` → `HealthResponse` (HTTP 200).
    - `POST /api/v1/chat` → resolve `thread_id`, adquire o `asyncio.Lock` da sessão e executa `run_turn` (quando `message is not None`) ou `resume_turn` (quando `approve is not None`) via `await asyncio.to_thread(...)` (sem bloquear o event loop), repassando `callbacks` (novo handler por requisição quando Langfuse ativo) e `model_name=settings.llm_model`, retornando `ChatResponse`.
  - **Segurança/LGPD em erros HTTP:**
    - Handler de `RequestValidationError` (HTTP 422) retornando payload estruturado sem ecoar o campo `input` (evitando vazar CPF/data de nascimento em respostas ou logs de validação).
    - Handler genérico de exceções não tratadas (HTTP 500) registrando apenas `type(exc).__name__` e retornando `{"detail": "Erro interno ao processar a solicitação."}`.
- **`Makefile`:**
  - Adicionar alvo `api`: `uv run uvicorn banco_agil.api.app:app --host 0.0.0.0 --port 8000` e incluir `api` em `.PHONY`.

### 3. Streamlit em Modo Híbrido (`src/banco_agil/ui/client.py` e `src/banco_agil/ui/app.py`)
- **`src/banco_agil/ui/client.py`:**
  - Implementar `HttpChatClient`:
    - Construtor `HttpChatClient(base_url: str, *, timeout_seconds: float = 60.0, transport: httpx.BaseTransport | None = None)`.
    - `send_message(*, thread_id: str, user_message: str) -> ChatResponse` (`POST /api/v1/chat`).
    - `resume(*, thread_id: str, approved: bool) -> ChatResponse` (`POST /api/v1/chat`).
    - `check_health() -> HealthResponse | None` (`GET /health`, retornando `None` em falha controlada).
    - Erros de rede/timeout/HTTP não-2xx levantam uma exceção limpa `ApiClientError` (sem cadeia de exceção contendo URLs/credenciais/PII) ou são tratados controladamente na UI retornando `ERROR_REPLY` e preservando o estado da sessão.
- **`src/banco_agil/ui/app.py`:**
  - Ler `Settings()` na inicialização da sessão:
    - Quando `settings.api_base_url` estiver vazio (`None`): manter o **modo direto em memória** exatamente como hoje (compatível com todos os testes existentes de `tests/test_ui.py`).
    - Quando `settings.api_base_url` estiver preenchido: inicializar `HttpChatClient(settings.api_base_url, timeout_seconds=settings.api_timeout_seconds)` em vez de `build_graph`/`CreditService`, executar turnos e retomadas HITL via HTTP, atualizar `thread_id`, `history`, `pending_confirmation`, `conversation_ended`, `last_telemetry`, `active_agent` e `langfuse_enabled` a partir de `ChatResponse`.
    - Em falha de comunicação HTTP durante um turno ou retomada HITL: registrar apenas `logger.error("Falha na comunicação com a API (%s)", type(exc).__name__)`, exibir `ERROR_REPLY` no histórico mantendo o chat utilizável (e preservando `pending_confirmation` caso a falha tenha ocorrido ao tentar retomar, para o cliente poder tentar novamente).
  - Indicar discretamente na sidebar o modo ativo (`Direto (em memória)` ou `API HTTP`) junto ao painel **"Telemetria do turno"** (sem exibir URL interna nem PII).

### 4. Empacotamento Docker (`.dockerignore`, `Dockerfile`, `docker-compose.yml`)
- **`.dockerignore`:** ignorar `.git`, `.venv`, `__pycache__`, caches (`.*_cache`), `.env`, `.env.*` (exceto `.env.example`), `tests/`, `docs/`, `evals/`, artefatos de cobertura e `.DS_Store`.
- **`Dockerfile`:**
  - Multi-stage baseado em `python:3.12-slim` usando `uv` (copiado da imagem oficial `ghcr.io/astral-sh/uv:0.6`) com `uv sync --frozen --no-dev --no-install-project` para cache eficiente de camadas e depois instalação do projeto (`src/`).
  - Copia `data/` e configura usuário não-root (`appuser`) com permissão de escrita em `/app/data`.
  - `ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1`.
  - `EXPOSE 8000` e `CMD ["uvicorn", "banco_agil.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]`.
- **`docker-compose.yml`:**
  - Serviço `api`: `build: .`, porta `${API_PORT:-8000}:8000`, `env_file` opcional (`.env` com `required: false`), volume `./data:/app/data`, `command` explícito com `--workers 1`, `healthcheck` via `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"`.
  - Serviço `ui`: `build: .`, porta `${UI_PORT:-8501}:8501`, `depends_on` aguardando `api` (`condition: service_healthy`), `environment` definindo `API_BASE_URL=http://api:8000`, `command` subindo Streamlit em `0.0.0.0:8501 --server.headless=true`.

### 5. Documentação Arquitetural (`docs/adr/008-api-fastapi-ui-hibrida-e-docker.md`)
Seguindo `docs/adr/000-template.md`, registrar:
- Arquitetura da API assíncrona (`FastAPI`, `lifespan`, `asyncio.to_thread`, lock por `thread_id`), UI híbrida (memória vs. HTTP) e empacotamento Docker com imagem única reutilizada pelos dois serviços e usuário não-root.
- Restrição explícita do MVP: `--workers 1` devido ao `InMemorySaver` e aos CSVs locais (ADRs 001 e 002).
- Alternativas descartadas: manter lógica acoplada apenas ao Streamlit; múltiplos workers com estado em memória; duas imagens Docker separadas no MVP.
- **Evolução e Escala:** múltiplas réplicas stateless (ECS Fargate / EKS atrás de ALB), checkpointer persistente (PostgreSQL/DynamoDB), banco relacional transacional, autenticação HTTP (OAuth2/JWT) e rate limiting distribuído (WAF/Redis), streaming SSE/WebSocket.

## Fora de Escopo
- Servidor `FastMCP` e integração MCP do Agente de Câmbio (`F10`).
- README final consolidado (`F11`).
- Autenticação HTTP/JWT e banco distribuído (documentados em *Evolução e Escala* no ADR 008).
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes (100% determinísticos, zero rede)
- `tests/test_api.py`:
  1. `GET /health` via `TestClient(app)` (exercitando o `lifespan` completo de startup e shutdown, verificando `observability.flush()` no encerramento e `langfuse_enabled` verdadeiro/falso).
  2. `POST /api/v1/chat` assíncrono via `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")` (sem sockets reais):
     - Geração automática de `thread_id` quando omitido e continuidade de sessão com o mesmo `thread_id`.
     - Fluxo completo autenticado + HITL (`message` → `pending_confirmation` → `approve: true` → aprovado e CSV atualizado, validando `telemetry` e zero PII na resposta).
     - Rejeição HTTP 422 quando `message` e `approve` são enviados juntos, ambos ausentes, `approve: "true"` / `approve: 1` (não-booleano estrito), campo extra ou `thread_id` em branco — confirmando que o corpo 422 **não ecoa PII** enviada no payload!
     - Erro inesperado no handler retorna HTTP 500 controlado sem vazar detalhes nem PII nos logs.
     - Serialização de requisições concorrentes no mesmo `thread_id` (via `asyncio.gather` com barreira/delay controlado no modelo fake).
     - Validação de que `create_handler()` cria handlers independentes por requisição quando configurado com factory.
- `tests/test_ui_http_mode.py` (ou extensão em `tests/test_ui.py`):
  1. `Settings`: valida `api_base_url` (normaliza barra final, rejeita esquemas inválidos como `ftp://` ou texto solto) e `api_timeout_seconds`.
  2. `HttpChatClient`: valida `send_message`, `resume`, `check_health` e tratamento controlado de timeout / HTTP 500 via `httpx.MockTransport` (zero sockets).
  3. Streamlit `AppTest` em modo HTTP (`API_BASE_URL=http://api.test`): envia mensagem, recebe pausa HITL com botões *Confirmar pedido* / *Cancelar*, confirma via HTTP, atualiza sidebar com modo `API HTTP`, `Langfuse ativo`/desativado e telemetria do turno, e trata falha HTTP exibindo `ERROR_REPLY` sem derrubar a UI.
- Validação estática dos arquivos Docker (`docker compose config --quiet` se Docker disponível, mais validação em teste ou revisão).

## Critérios de Aceite
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [ ] `make evals` 100% verde (12/12 cenários, 0 vazamentos de PII).
- [ ] `docker compose config --quiet` válido (sem erros de sintaxe no `docker-compose.yml`).
- [ ] Cobertura ≥ 95% nos módulos novos (`api/` e `ui/client.py`) e cobertura geral mantida ≥ 98%.
- [ ] `docs/adr/008-api-fastapi-ui-hibrida-e-docker.md` criado seguindo `docs/adr/000-template.md`.
- [ ] Commits pequenos em Conventional Commits na branch `feat/f09-fastapi-docker` (incluindo `plan.md`).

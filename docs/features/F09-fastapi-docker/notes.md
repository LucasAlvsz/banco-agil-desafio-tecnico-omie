# Notas — F09: FastAPI, UI Híbrida e Docker

## Entregue
- API REST assíncrona em FastAPI (`src/banco_agil/api/app.py`, `src/banco_agil/api/schemas.py`, alvo `make api`):
  - `GET /health` e `POST /api/v1/chat` (`ChatRequest` com `StrictBool` em `approve`, geração automática de `thread_id` UUID v4, `ChatResponse` com `pending_confirmation`, `telemetry` e `langfuse_enabled`).
  - `lifespan` inicializando dependências no startup e executando `flush()` do Langfuse e drenagem de workers pendentes no shutdown (import sem efeitos colaterais).
  - Serialização por `thread_id` (`asyncio.Lock` com `asyncio.shield` + `add_done_callback` para suportar cancelamento HTTP sem liberar o lock antes da worker thread concluir), `CreditService._write_lock` para serializar atualizações concorrentes nos CSVs e `LangfuseObservability` criando handlers independentes por requisição.
  - Tratamento de erros 422/500 sem ecoar `input`, chaves extras desconhecidas nem stack traces com PII.
- Streamlit em modo híbrido (`src/banco_agil/ui/client.py` + `src/banco_agil/ui/app.py`): modo direto em memória (padrão) e modo cliente HTTP via `API_BASE_URL` / `API_TIMEOUT_SECONDS`, preservando HITL, telemetria na sidebar e recuperação controlada em falhas HTTP.
- Empacotamento Docker (`Dockerfile` multi-stage `python:3.12-slim` + `ghcr.io/astral-sh/uv:0.10` com usuário não-root `appuser`, `.dockerignore` e `docker-compose.yml` para `api` `--workers 1` com healthcheck + `ui`).
- Decisões e caminhos de escala registrados em [ADR 008](../../adr/008-api-fastapi-ui-hibrida-e-docker.md).

## Revisão
- Cross-model review identificou incompatibilidade de `ghcr.io/astral-sh/uv:0.6` com `uv.lock` (`revision = 3`); corrigido para `ghcr.io/astral-sh/uv:0.10` (`d7663db`) e validado com `docker build` + import smoke no container (`CONTAINER_IMPORT_OK`). Aprovado (LGTM).

## Verificação
- `make check`: `ruff`, `mypy --strict` (31 arquivos) e 406 testes passando (99% cobertura total; 100% em `api/app.py`, `api/schemas.py`, `ui/client.py`, `config.py`, `services.py` e `observability.py`; 98% em `ui/app.py`).
- `make evals`: 12/12 cenários aprovados (100%), rotas 100%, 0 vazamentos de PII.
- `docker compose config --quiet` e `docker build` verdes.

# Notas — F12: Runtime do Atendimento e Seam de Observabilidade

## Entregue
- **Seam de observabilidade por turno (`src/banco_agil/infrastructure/observability.py`):**
  - Quebra do ciclo `observability → agents` com remoção do import de `AgentName`, `TurnTelemetry.agent_route: tuple[str, ...] = ()` e `TurnTelemetryCallback(tracked_nodes=..., default_model_name=...)` mantendo deduplicação de nós consecutivos.
  - Simplificação de `LangfuseObservability` (sem `_handler` mutável nem `Lock`), criando um handler independente por turno via `handler_factory` e preservando degradação segura no startup e em `flush()`.
  - Introdução de `TurnObservability` e `TurnScope`, centralizando cronômetro (`perf_counter`), montagem de `callbacks`, `metadata` Langfuse apenas quando ativo (sem `langfuse_user_id`) e cálculo final de `TurnTelemetry`.
- **Integração limpa em `src/banco_agil/agents/graph.py`:** remoção de `perf_counter`, `TurnTelemetryCallback` e chaves literais do Langfuse em `_execute_turn`, preservando a interface pública (`build_graph`, `run_turn`, `resume_turn`), guardrails, `GraphRecursionError` recovery e HITL.
- **Módulo profundo `ConversationRuntime` (`src/banco_agil/runtime.py`):** composition root síncrono único (`from_settings`), despacho de turnos (`run_turn`, `resume_turn`), leitura encapsulada de estado (`get_state`) e controle de ownership de observabilidade em `close()` / context manager (`__enter__`/`__exit__`) idempotente.
- **Migração dos três callers e correção de bugs:**
  - `src/banco_agil/api/app.py` e `src/banco_agil/api/schemas.py`: `create_app(settings=..., runtime=...)` com `ApiRuntimeState` isolando concorrência assíncrona (`thread_locks`, `creation_lock`, `workers`), `close()` condicional no `lifespan` e `TurnTelemetry(LocalTurnTelemetry)` preservando `tuple[AgentName, ...]` no schema OpenAPI.
  - `src/banco_agil/ui/app.py`: modo direto migrado para `ConversationRuntime.from_settings(settings)`, corrigindo o bug de `flush()` ausente ao clicar em *Nova conversa* (`previous.close()`).
  - `src/banco_agil/evals/runner.py`: modo `live` com `TurnObservability` único e `flush()` garantido no `finally`, criando um handler Langfuse independente por turno/cenário (corrigindo o reuso de handler) e eliminando a duplicação do composition root de `CreditService`.
- **Glossário de domínio (`GLOSSARY.md`):** criado na raiz do repositório contendo exclusivamente linguagem de domínio.
- **Testes (*replace, don't layer*):** nova suíte `tests/test_runtime.py` e atualização de `tests/test_observability.py`, `tests/test_api.py`, `tests/test_ui.py`, `tests/test_ui_http_mode.py` e `tests/test_evals.py` com regressões explícitas para `flush()` na UI e handlers independentes em `evals/runner.py`.

## Revisão
- Review frio em dois eixos (`/code-review` — Standards e Spec) sobre `main...5280aae`: 0 violações hard, apontando dois polimentos aprovados (`CreditService` duplicado em `evals/runner.py` e sombreamento `Runtime`/`runtime` em `api/app.py`).
- Segundo `/code-review` sobre `5280aae...6234315` contra o handoff de ajustes: aprovado limpo nos dois eixos (0 violações hard, 0 desvios de spec).

## Verificação
- `make check`: `ruff check`, `ruff format --check`, `mypy src` (`--strict`, 35 arquivos) e 443 testes passando (99% cobertura total; 100% em `runtime.py`, `observability.py`, `graph.py`, `api/app.py` e `api/schemas.py`).
- `make evals`: 12/12 cenários aprovados (100%), rotas 100%, 0 vazamentos de PII.

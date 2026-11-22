# Plano — F12: Runtime do Atendimento e Seam de Observabilidade

## Objetivo

Consolidar a composição, o despacho de turnos e o ciclo de vida de observabilidade do Atendimento atrás de um único módulo profundo (`ConversationRuntime` em `src/banco_agil/runtime.py`) e fechar a mecânica de telemetria local + Langfuse atrás de um seam por turno em `src/banco_agil/infrastructure/observability.py`.

A mudança resolve quatro dores concretas da arquitetura atual:

1. **Duplicação do composition root em 3 callers:** `api/app.py`, `ui/app.py` e `evals/runner.py` montam separadamente repositórios CSV, `CreditService`, `exchange_provider`, LLM, Langfuse e `build_graph`.
2. **Bug de `flush()` ausente na UI Streamlit:** ao clicar em *Nova conversa* no modo direto, `ui/app.py` descarta `langfuse_observability` sem chamar `flush()`.
3. **Bug de handler Langfuse reutilizado em `evals/runner.py`:** `run_evals` cria um único handler fora do loop de cenários e o reutiliza em todos os turnos dos 12 cenários.
4. **Ciclo `observability → agents` e vazamento de Langfuse em `agents/graph.py`:** `infrastructure/observability.py` importa `AgentName` de `agents/state.py`, `agents/graph.py` fixa `langfuse_session_id`/`langfuse_tags`, cronometra latência com `perf_counter()` e manipula `TurnTelemetryCallback`, enquanto `LangfuseObservability` mantém um slot mutável `_handler` + `Lock`.

## Escopo

### 1. Seam de Observabilidade por Turno (`src/banco_agil/infrastructure/observability.py`)

- **Quebrar o ciclo `observability → agents`:**
  - Remover `from banco_agil.agents.state import AgentName` e `AGENT_NAMES = get_args(AgentName)`.
  - Alterar `TurnTelemetry.agent_route` para `tuple[str, ...] = ()` (preserva exatamente o contrato JSON/schema).
  - Parametrizar `TurnTelemetryCallback(tracked_nodes: Collection[str], default_model_name: str | None = None)` para rastrear apenas `langgraph_node in tracked_nodes` (mantendo a deduplicação de nós consecutivos).
- **Simplificar `LangfuseObservability`:**
  - Remover o slot mutável `_handler` e `_handler_lock` (`Lock`).
  - Construtor enxuto: `LangfuseObservability(client: Langfuse, handler_factory: Callable[[], BaseCallbackHandler])`.
  - `create_handler() -> BaseCallbackHandler` sempre invoca `self._handler_factory()`, garantindo um handler independente por turno.
  - `flush() -> None` preserva o `try/except` registrando apenas `logger.warning("Langfuse indisponível (%s)", type(exc).__name__)`.
  - `create_langfuse_observability(settings: Settings) -> LangfuseObservability | None` mantém o probe único de validação no startup (executando `handler_factory()` uma vez dentro do `try/except` para degradar com segurança a `None` em erro de inicialização) sem guardar esse handler como estado mutável.
- **Introduzir `TurnObservability` e `TurnScope`:**
  - `TurnObservability(langfuse: LangfuseObservability | None = None)`:
    - Propriedade `langfuse_enabled -> bool` (`self._langfuse is not None`).
    - `start_turn(*, thread_id: str, tracked_nodes: Collection[str], default_model_name: str | None = None, extra_callbacks: Sequence[BaseCallbackHandler] = ()) -> TurnScope`:
      - Inicia o cronômetro (`perf_counter()`), cria `TurnTelemetryCallback(tracked_nodes=tracked_nodes, default_model_name=default_model_name)` e, se `self._langfuse` estiver ativo, cria um handler novo via `self._langfuse.create_handler()`.
      - Monta `callbacks` como `(telemetry_callback, *langfuse_callbacks, *extra_callbacks)`.
      - Monta `metadata`: `{"langfuse_session_id": thread_id, "langfuse_tags": ["banco-agil"]}` quando `self._langfuse` estiver ativo (jamais inclui `langfuse_user_id`), ou `{}` no modo local-only.
    - `flush() -> None`: delega para `self._langfuse.flush()` quando ativo; no-op caso contrário.
  - `TurnScope`:
    - Expõe `callbacks: tuple[BaseCallbackHandler, ...]` e `metadata: Mapping[str, Any]` (ou `dict[str, Any]` somente-leitura/cópia segura).
    - `finish(*, fallback_node: str, guardrail_blocked: bool = False) -> TurnTelemetry`: calcula `(perf_counter() - started) * 1000`, aplica `fallback_node` quando `agent_route` estiver vazio e retorna `TurnTelemetry`.

### 2. Integração Limpa em `src/banco_agil/agents/graph.py`

- **Preservar a interface de agentes:** manter `build_graph`, `run_turn` e `resume_turn` como interface pública para que os testes focados de agentes continuem leves e diretos.
- **Remover o encanamento de observabilidade vazado:**
  - Remover `perf_counter`, `TurnTelemetryCallback`, fallback manual de `agent_route` e as chaves literais `langfuse_session_id`/`langfuse_tags` de `_execute_turn`.
  - `run_turn` e `resume_turn` passam a aceitar `observability: TurnObservability | None = None` (padrão `None`, usando um `TurnObservability()` local-only para o turno), mantendo `callbacks: Sequence[BaseCallbackHandler] = ()` e `model_name: str | None = None`.
  - `_execute_turn` abre `scope = (observability or TurnObservability()).start_turn(thread_id=thread_id, tracked_nodes=AGENT_NODES, default_model_name=model_name, extra_callbacks=callbacks)`, invoca o grafo com `callbacks=list(scope.callbacks)` e `metadata=dict(scope.metadata)` (quando houver metadados), e conclui com `scope.finish(fallback_node=active_agent, guardrail_blocked=guardrail_blocked)`.
  - Preservar integralmente a proteção de `recursion_limit < 2`, guardrails, `GraphRecursionError` recovery, `sanitize_reply` e validação HITL.

### 3. Novo Módulo Profundo `src/banco_agil/runtime.py` (`ConversationRuntime`)

- **Módulo estritamente síncrono** (concorrência HTTP assíncrona permanece isolada em `src/banco_agil/api/app.py`).
- **Interface pública:**
  - `ConversationRuntime(graph: ConversationGraph, *, model_name: str | None = None, observability: TurnObservability | None = None, owns_observability: bool = False)`:
    - Quando `observability is None`, cria `TurnObservability()` local-only.
  - `@classmethod def from_settings(cls, settings: Settings | None = None, *, model: BaseChatModel | None = None, credit_service: CreditService | None = None, exchange_provider: ExchangeRateProvider | None = None, observability: TurnObservability | None = None, checkpointer: BaseCheckpointSaver[Any] | None = None) -> ConversationRuntime`:
    - Resolve `runtime_settings = settings if settings is not None else Settings()`.
    - Instancia `CreditService` com os três repositórios CSV em `runtime_settings.data_dir` quando `credit_service is None`.
    - Instancia `create_exchange_provider(runtime_settings)` quando `exchange_provider is None`.
    - Instancia `create_chat_model(runtime_settings)` quando `model is None`.
    - Se `observability is None`, cria `TurnObservability(create_langfuse_observability(runtime_settings))` com `owns_observability=True`; se `observability` foi injetada, define `owns_observability=False`.
    - Constrói `build_graph(chat_model, service, exchange_provider=provider, checkpointer=checkpointer)` e retorna `ConversationRuntime` com `model_name=runtime_settings.llm_model`.
  - `langfuse_enabled -> bool` (`property`).
  - `run_turn(*, thread_id: str, user_message: str, recursion_limit: int = DEFAULT_RECURSION_LIMIT, callbacks: Sequence[BaseCallbackHandler] = ()) -> TurnResult`.
  - `resume_turn(*, thread_id: str, approved: bool, recursion_limit: int = DEFAULT_RECURSION_LIMIT, callbacks: Sequence[BaseCallbackHandler] = ()) -> TurnResult`.
  - `get_state(thread_id: str) -> ConversationState`: leitura somente-leitura do snapshot atual da thread (`cast(ConversationState, self._graph.get_state({"configurable": {"thread_id": thread_id}}).values)`), sem expor `CompiledStateGraph`.
  - `close() -> None`: chama `self._observability.flush()` **somente** quando `self._owns_observability` for `True` (idempotente).
  - Suporte a context manager (`__enter__` / `__exit__`) delegando para `close()`.

### 4. Migração dos Três Callers

- **API FastAPI (`src/banco_agil/api/app.py`):**
  - Remover imports de `CreditService`, repositórios CSV, `create_chat_model`, `create_exchange_provider`, `create_langfuse_observability`, `LangfuseObservability`, `build_graph`, `run_turn`, `resume_turn`.
  - Atualizar `create_app(*, settings: Settings | None = None, runtime: ConversationRuntime | None = None) -> FastAPI`.
  - `Runtime` interno da API segura `conversation: ConversationRuntime`, `owns_runtime: bool` e apenas o estado de concorrência assíncrona (`thread_locks`, `creation_lock`, `workers`).
  - No `lifespan`: se `runtime is None`, cria `ConversationRuntime.from_settings(settings)` (`owns_runtime=True`); no `finally`, aguarda `workers`, remove `app.state.runtime` e chama `conversation.close()` somente quando `owns_runtime` for `True` (regra consistente: quem cria, fecha).
  - Preservar intactos: `configure_logging()`, handlers 422/500 sem PII, geração de UUID v4 quando `thread_id` é omitido, lock por `thread_id`, `asyncio.to_thread` e `asyncio.shield` no cancelamento HTTP.
- **UI Streamlit (`src/banco_agil/ui/app.py`):**
  - Migrar o modo direto (`settings.api_base_url is None`) para `ConversationRuntime.from_settings(settings)`, eliminando os 6 imports de infraestrutura/domínio e o uso direto de `build_graph`/`run_turn`/`resume_turn`/`create_langfuse_observability`.
  - Armazenar `st.session_state.runtime` (em vez de `graph`, `llm_model` e `langfuse_observability`) e derivar `st.session_state.langfuse_enabled = runtime.langfuse_enabled`.
  - Ao clicar em **Nova conversa**, se houver um `runtime` anterior em `st.session_state`, chamar `previous.close()` antes de descartá-lo (corrigindo o bug de `flush()` ausente).
  - Manter o ramo `http_client` funcionando sem alterações de contrato (a unificação atrás de `ChatClient` pertence à F13).
- **Runner de Evals (`src/banco_agil/evals/runner.py`):**
  - Remover a criação única de `callbacks = (observability.create_handler(),)` fora do loop de cenários e a chamada direta a `build_graph`/`run_turn`/`resume_turn`.
  - Em `run_evals`: no modo `live`, instanciar `observability = TurnObservability(create_langfuse_observability(settings))` uma única vez e chamar `observability.flush()` no `finally`; no modo `offline`, usar `observability = TurnObservability()` (sem tocar em Langfuse).
  - Em `_run_scenario`: abrir `with ConversationRuntime.from_settings(scenario_settings, model=model, credit_service=service, exchange_provider=..., observability=observability) as runtime:` (como `observability` foi injetada, `close()` não dá `flush()` prematuro entre cenários, e cada turno recebe seu próprio handler Langfuse via `start_turn`).
  - Substituir `graph.get_state(config).values` por `runtime.get_state(thread_id)`.

## Fora de Escopo

- Unificação dos modos HTTP e local da UI Streamlit atrás do seam `ChatClient` (`F13`).
- Contratos tipados de HITL, limpeza de texto retornada por `guardrails.py`, deduplicação do gate de auth e remoção do import `McpExchangeRateProvider` de `agents/graph.py` (`F14`).
- Colocalização do protocolo MCP de câmbio (`F15`).
- Refatoração dos helpers de privacidade/CSV em `evals/runner.py` (Spike — Candidato 6).
- Criação de ADR novo (a F12 reforça os ADRs 007 e 008 sem introduzir trade-off irreversível ou conflito arquitetural).
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree (essas atualizações — incluindo o registro do Spike do Candidato 6 — ocorrem no fechamento ao integrar na `main`).

## Seams de Teste Pré-Acordados (para execução com `/tdd`)

Seguindo o princípio *the interface is the test surface* e *replace, don't layer*:

| Seam | Interface sob teste | O que captura | O que fica fora (coberto por outro seam) |
| :--- | :--- | :--- | :--- |
| **1. `TurnObservability` + `TurnScope`** (`tests/test_observability.py`) | `TurnObservability.start_turn(...) -> TurnScope`, `TurnScope.finish(...) -> TurnTelemetry`, `flush()`, `create_langfuse_observability(settings)`, `estimate_cost_usd`, hooks `langfuse_mask_*` | Latência, tokens, custo, rota filtrada por `tracked_nodes`, `fallback_node`, `guardrail_blocked`, metadados Langfuse **apenas** quando ativo, criação de handler novo por turno (zero reuso), degradação segura e mascaramento PII sem ciclo de import para `agents` | Concorrência HTTP e montagem de CSVs |
| **2. `ConversationRuntime`** (`tests/test_runtime.py` — novo) | `ConversationRuntime.from_settings(...)`, `run_turn`, `resume_turn`, `get_state`, `langfuse_enabled`, `close()` / context manager | Wiring completo (CSV + Crédito + Câmbio + LLM + Observabilidade + Grafo), propagação de `model_name`, ownership de `flush()` (criado vs. injetado), criação de handler independente por turno e encapsulamento de estado | Serialização HTTP assíncrona e renderização Streamlit |
| **3. API FastAPI** (`tests/test_api.py`) | `create_app(settings=..., runtime=...)` via `TestClient` / `httpx.AsyncClient` (`/health`, `/api/v1/chat`) | Inicialização lazy no `lifespan`, `flush()` no shutdown apenas quando a API criou o runtime, fluxo HITL ponta a ponta, sanitização 422/500 sem PII, serialização por `thread_id`, `asyncio.shield` no cancelamento e handlers Langfuse independentes em turnos concorrentes | Detalhes internos de `TurnTelemetryCallback` |
| **4. UI Streamlit** (`tests/test_ui.py`, `tests/test_ui_http_mode.py`) | `AppTest` cruzando `ConversationRuntime.from_settings` (modo direto) e `HttpChatClient` (modo HTTP) | Fluxo completo de chat/HITL/guardrail/erro, sidebar de telemetria, chamada de `runtime.close()` ao clicar em *Nova conversa* (regressão do bug de `flush()`), ausência de inicialização local no modo HTTP | Contrato interno de `agents/graph.py` |
| **5. Evals** (`tests/test_evals.py`) | `run_evals(mode="offline" \| "live", ...)` e CLI `main(...)` | 12/12 cenários offline sem inicializar Langfuse, modo `live` criando um handler Langfuse distinto por turno/cenário e chamando `flush()` uma única vez no fim (inclusive em erro) | Regras de negócio individuais de cada agente |

### Estratégia *Replace, Don't Layer* nos Testes Existentes

- **`tests/test_observability.py`**: substituir testes que dependiam de `graph.py` injetar `langfuse_session_id` sem Langfuse configurado por testes cruzando `TurnObservability` / `ConversationRuntime` (verificando ausência de chaves Langfuse no modo local e presença + handlers distintos por turno no modo Langfuse); validar que `observability.py` não importa `banco_agil.agents`.
- **`tests/test_api.py`**: substituir `create_app(graph=...)`, `create_app(graph=..., observability=...)` e monkeypatches de `api.create_*` / `api.run_turn` por injeção de `ConversationRuntime` (ou wrapper/adapter no seam `runtime=`) e testes pelo `create_app(settings=...)`.
- **`tests/test_ui.py` e `tests/test_ui_http_mode.py`**: remover acessos a `app.session_state.graph.update_state(...)` e monkeypatches de `ui.app.create_chat_model` / `ui.app.create_exchange_provider` / `ui.app.create_langfuse_observability` / `ui.app.build_graph`; passar a cruzar o seam `ConversationRuntime.from_settings` (com modelo roteirizado + `exchange_provider` ou `observability` injetados) e adicionar cobertura explícita verificando que **Nova conversa** chama `flush()` no Langfuse.
- **`tests/test_evals.py`**: atualizar os testes de modo `live` para verificar não apenas `flush()` único ao final, mas também que múltiplos turnos/cenários geram handlers independentes (regressão do bug corrigido), e remover monkeypatch em `runner.run_turn` (substituindo por injeção limpa no seam).

## Sequência de Fatias Verticais Sugerida (`/tdd`: red → green por fatia)

1. **Fatia 1 — Seam `TurnObservability` / `TurnScope` sem ciclo `observability → agents`:**
   - Red → green em `tests/test_observability.py` → implementar `TurnObservability`, `TurnScope`, `LangfuseObservability` sem `_handler`/`Lock`, `TurnTelemetry.agent_route: tuple[str, ...]` e integrar `agents/graph.py`.
2. **Fatia 2 — Módulo `ConversationRuntime`:**
   - Red → green em `tests/test_runtime.py` → implementar `src/banco_agil/runtime.py` (`from_settings`, `run_turn`, `resume_turn`, `get_state`, `close`/context manager, ownership de `flush()`).
3. **Fatia 3 — Migração da API FastAPI:**
   - Atualizar `tests/test_api.py` para o seam `create_app(settings=..., runtime=...)` → migrar `src/banco_agil/api/app.py`.
4. **Fatia 4 — Migração da UI Streamlit (modo direto + bugfix `flush()`):**
   - Atualizar `tests/test_ui.py` e `tests/test_ui_http_mode.py` (incluindo regressão de `flush()` em *Nova conversa*) → migrar `src/banco_agil/ui/app.py`.
5. **Fatia 5 — Migração do `evals/runner.py` (bugfix handler compartilhado):**
   - Atualizar `tests/test_evals.py` (incluindo regressão de handlers distintos por turno/cenário + `flush()` único) → migrar `src/banco_agil/evals/runner.py`.
6. **Gate final no worktree:** rodar `make check` e `make evals` (100% verdes).

## Critérios de Aceite

- [ ] `src/banco_agil/infrastructure/observability.py` não importa nenhum módulo de `banco_agil.agents`.
- [ ] `src/banco_agil/agents/graph.py` não referencia `perf_counter`, `TurnTelemetryCallback`, `langfuse_session_id` nem `langfuse_tags`, preservando `build_graph`, `run_turn` e `resume_turn`.
- [ ] `LangfuseObservability` não mantém `_handler` mutável nem `Lock`, criando um handler independente por turno via `handler_factory`.
- [ ] `src/banco_agil/runtime.py` (`ConversationRuntime`) concentra o composition root e respeita ownership de `flush()` em `close()` / `__exit__()`.
- [ ] `api/app.py`, `ui/app.py` (modo direto) e `evals/runner.py` operam exclusivamente sobre `ConversationRuntime`, sem duplicar wiring de repositórios/LLM/observabilidade.
- [ ] Bugs corrigidos com testes de regressão: `ui/app.py` chama `flush()` ao clicar em *Nova conversa*; `evals/runner.py` cria um handler Langfuse distinto por turno/cenário e chama `flush()` uma única vez ao final.
- [ ] `GLOSSARY.md` presente na raiz do repositório contendo exclusivamente linguagem de domínio.
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`, cobertura geral mantida ≥ 98%).
- [ ] `make evals` 100% verde (12/12 cenários, 0 vazamentos de PII).
- [ ] Commits pequenos em Conventional Commits na branch `feat/f12-runtime-observability`, sem commitar arquivos de tooling local.

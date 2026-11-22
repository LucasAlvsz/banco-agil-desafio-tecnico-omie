# Plano — F13: Seam `ChatClient` na UI Streamlit

## Objetivo

Unificar os modos HTTP e direto (em memória) da interface Streamlit atrás de um único seam `ChatClient` (`HttpChatClient` + `LocalChatClient` envolvendo `ConversationRuntime`) em `src/banco_agil/ui/client.py`, transformando `src/banco_agil/ui/app.py` em camada de apresentação pura com zero ramificações de modo e atualizando o [ADR 002](../../adr/002-camada-llm-litellm-e-walking-skeleton.md) para refletir a limpeza de thread em *Nova conversa* sem reconstrução da pilha local.

A mudança resolve quatro dores concretas levantadas no Candidato 2 da revisão arquitetural:

1. **Ramificações de modo duplo em `ui/app.py`:** a UI alterna entre `http_client` e `runtime` em quatro pontos (inicialização, despacho de turno, *Nova conversa* e legenda da sidebar) e mantém chaves paralelas em `st.session_state`.
2. **Inspeção de tipo em tempo de execução:** `_record_reply` faz `isinstance(result, ChatResponse)` porque o modo HTTP retorna `ChatResponse` enquanto o modo direto retorna `TurnResult`.
3. **Desmontagem de toda a pilha em *Nova conversa*:** o modo direto descarta e reconstrói `ConversationRuntime` (repositórios CSV, `CreditService`, MCP de câmbio, `ChatLiteLLM`, Langfuse e grafo compilado) a cada clique em *Nova conversa*, quando basta limpar a thread anterior no checkpointer em memória (`delete_thread`) e disparar `flush()` de observabilidade.
4. **Testes acoplados a detalhes internos da UI:** `tests/test_ui.py` lê `app.session_state.runtime.get_state(...)` e `app.session_state.runtime`, e `tests/test_ui_http_mode.py` faz monkeypatch diretamente em `banco_agil.ui.app.HttpChatClient`.

## Escopo

### 1. Limpeza de Thread em `ConversationRuntime` (`src/banco_agil/runtime.py`)

- Adicionar `reset_thread(self, thread_id: str) -> None`:
  - Se `isinstance(self._graph.checkpointer, BaseCheckpointSaver)`, chama `self._graph.checkpointer.delete_thread(thread_id)` para remover checkpoints, writes e blobs daquela thread no `InMemorySaver` (mantendo memória limitada).
  - Se `self._owns_observability` for `True`, chama `self._observability.flush()` **sem** alterar `self._owns_observability` para `False`, permitindo que cliques subsequentes em *Nova conversa* continuem disparando `flush()` dos traces do Atendimento encerrado.
  - Deve ser seguro e idempotente mesmo se `thread_id` ainda não tiver recebido nenhum Turno.
- Preservar intactos `from_settings`, `run_turn`, `resume_turn`, `get_state`, `langfuse_enabled`, `close()` (idempotente, encerra ownership) e `__enter__`/`__exit__`.

### 2. Seam `ChatClient`, Adaptadores e Factory (`src/banco_agil/ui/client.py`)

- **Contrato de erro do seam:**
  - Introduzir `ChatClientError(Exception)` como exceção neutra capturada pela UI.
  - Manter `ApiClientError(ChatClientError)` como subclasse específica do adaptador HTTP (preservando compatibilidade e semântica de falha HTTP/transporte/JSON sem PII nem URL).
- **Protocolo `ChatClient(Protocol)`:**
  - `@property def mode_label(self) -> str`
  - `@property def langfuse_enabled(self) -> bool`
  - `def send_message(self, *, thread_id: str, user_message: str) -> ChatResponse`
  - `def resume(self, *, thread_id: str, approved: bool) -> ChatResponse`
  - `def reset_conversation(self, thread_id: str) -> None`
  - `def close(self) -> None`
- **Adaptador `HttpChatClient`:**
  - Preservar construtor sem I/O de rede: `HttpChatClient(base_url, *, timeout_seconds=60.0, transport=None)`, inicializando `self._langfuse_enabled = False`.
  - `mode_label` retorna `"API HTTP"`.
  - `langfuse_enabled` retorna `self._langfuse_enabled`.
  - `check_health() -> HealthResponse | None` atualiza `self._langfuse_enabled = health.langfuse_enabled` quando `health is not None` (ou `False` em falha) e retorna `health`.
  - `send_message` e `resume` retornam `ChatResponse`, atualizam `self._langfuse_enabled = response.langfuse_enabled` e continuam convertendo falhas de transporte/HTTP/validação em `ApiClientError` sem PII, sem URL e sem `__cause__`/`__context__`.
  - `reset_conversation(thread_id: str) -> None`: no-op (a API controla checkpoints e traces; não dispara nova chamada HTTP).
  - `close() -> None`: no-op (mantém `httpx.Client` efêmero por requisição, seguro no modelo de ciclo de vida do Streamlit).
- **Adaptador `LocalChatClient`:**
  - Construtor: `LocalChatClient(runtime: ConversationRuntime, *, owns_runtime: bool = False)`.
  - `@classmethod def from_settings(cls, settings: Settings | None = None) -> Self`: cria `ConversationRuntime.from_settings(settings)` com `owns_runtime=True`.
  - `mode_label` retorna `"Direto (em memória)"`.
  - `langfuse_enabled` delega para `self._runtime.langfuse_enabled`.
  - `send_message(*, thread_id: str, user_message: str) -> ChatResponse`: executa `self._runtime.run_turn(thread_id=thread_id, user_message=user_message)` e retorna `ChatResponse(**result.model_dump(), thread_id=thread_id, langfuse_enabled=self._runtime.langfuse_enabled)`.
  - `resume(*, thread_id: str, approved: bool) -> ChatResponse`: executa `self._runtime.resume_turn(thread_id=thread_id, approved=approved)` e retorna `ChatResponse(**result.model_dump(), thread_id=thread_id, langfuse_enabled=self._runtime.langfuse_enabled)`.
  - `reset_conversation(thread_id: str) -> None`: delega para `self._runtime.reset_thread(thread_id)`.
  - `close() -> None`: fecha `self._runtime.close()` somente quando `self._owns_runtime` for `True` (idempotente, seguindo a convenção da F12 *quem cria, fecha*).
  - Suporte a context manager (`__enter__`/`__exit__`) delegando para `close()`.
- **Factory `create_chat_client(settings: Settings | None = None) -> ChatClient`:**
  - Resolve `resolved = settings if settings is not None else Settings()`.
  - Se `resolved.api_base_url is not None`: instancia `HttpChatClient(resolved.api_base_url, timeout_seconds=resolved.api_timeout_seconds)`, chama `client.check_health()` uma vez para popular `langfuse_enabled` inicial e retorna `client`.
  - Caso contrário: retorna `LocalChatClient.from_settings(resolved)`.

### 3. UI Streamlit como Apresentação Pura (`src/banco_agil/ui/app.py`)

- Remover imports de `ConversationRuntime`, `HttpChatClient`, `ApiClientError`, `Settings` e eliminar qualquer condicional entre modo HTTP e direto.
- Importar apenas `ChatClient`, `ChatClientError` e `create_chat_client` de `banco_agil.ui.client` (além de `ChatResponse` apenas para anotação de tipo de `_record_reply`, se necessário — sem `isinstance`).
- Manter uma única referência de cliente em `st.session_state.client` (eliminando `st.session_state.runtime`, `st.session_state.http_client` e `st.session_state.langfuse_enabled`).
- `_record_reply(result: ChatResponse) -> None`: atualiza `thread_id`, `history`, `pending_confirmation`, `conversation_ended`, `last_telemetry` e `active_agent` de forma uniforme, sem `isinstance`.
- `_submit_turn`: usa exclusivamente `client: ChatClient = st.session_state.client` (`client.send_message(...)` / `client.resume(...)`), captura `ChatClientError`, registra log neutro sem PII/URL (`logger.error("Falha ao enviar turno (%s)", type(exc).__name__)`), adiciona `ERROR_REPLY` ao histórico e preserva `pending_confirmation` para permitir nova tentativa.
- `_show_telemetry`: recebe `mode_label: str` e `langfuse_enabled: bool` diretamente de `client.mode_label` e `client.langfuse_enabled`.
- **Inicialização e *Nova conversa*:**
  - Em `new_conversation`: se já existir `client` e `thread_id` em `st.session_state`, chama `client.reset_conversation(previous_thread_id)` mantendo o mesmo `st.session_state.client` vivo; em seguida gera novo `thread_id` e limpa `history`, `conversation_ended`, `pending_confirmation`, `last_telemetry` e `active_agent`.
  - Na primeira carga (`"thread_id" not in st.session_state`): inicializa `thread_id` e o estado visual do Atendimento sem chamar `reset_conversation`.
  - Se `"client" not in st.session_state`: chama `create_chat_client()` dentro do `try/except Exception` existente, preservando log sanitizado (`"Falha na inicialização do atendimento (%s)"`), mensagem amigável em `st.error(...)` e `st.stop()`.

### 4. Atualização do ADR 002 (`docs/adr/002-camada-llm-litellm-e-walking-skeleton.md`)

- Preservar o texto histórico da decisão original (F02) e adicionar uma nota explícita e datada de evolução (**Atualização (F13)**) documentando que:
  - A UI Streamlit agora interage com o atendimento atrás do seam `ChatClient` (`HttpChatClient` e `LocalChatClient`).
  - No modo direto, **Nova conversa** reutiliza o mesmo `LocalChatClient`/`ConversationRuntime` na sessão Streamlit sem reconstruir grafo, LLM, MCP ou Langfuse, limpando o estado da thread anterior no `InMemorySaver` via `delete_thread(thread_id)` e disparando `flush()` de observabilidade para preservar memória limitada e entrega de traces.

## Fora de Escopo

- Contratos tipados de HITL, retorno limpo de `evaluate_user_input`, deduplicação do gate de autenticação e remoção do import `McpExchangeRateProvider` de `agents/graph.py` (`F14`).
- Colocalização do protocolo MCP de câmbio (`F15`).
- Refatoração dos helpers de privacidade/CSV em `evals/runner.py` (Spike — Candidato 6).
- Alteração em `GLOSSARY.md` (a F13 não introduz nem redefine termos de domínio).
- Alteração em `api/app.py`, `agents/graph.py` ou `evals/runner.py`.
- Reuso de conexão persistente `httpx.Client` em `HttpChatClient` (Streamlit não oferece hook determinístico no encerramento de aba).
- Edição de `docs/backlog.md`, `docs/spec.md` e `AGENTS.md` dentro do worktree (atualizados no fechamento ao integrar na `main`).

## Seams de Teste Pré-Acordados (para execução com `/tdd`)

Seguindo *the interface is the test surface* e *replace, don't layer*:

| Seam | Interface sob teste | O que captura | O que fica fora (coberto por outro seam) |
| :--- | :--- | :--- | :--- |
| **1. `ConversationRuntime.reset_thread`** (`tests/test_runtime.py`) | `ConversationRuntime.reset_thread(thread_id)` | Remoção real dos checkpoints/estado da thread no `InMemorySaver` (`get_state(thread_id)` volta vazio), `flush()` disparado apenas quando `owns_observability=True` (sem encerrar ownership — múltiplos resets continuam dando `flush()`), idempotência em `thread_id` inexistente | Renderização Streamlit e protocolo HTTP |
| **2. Adaptadores e Factory `ChatClient`** (`tests/test_ui_client.py` — novo, absorvendo os testes de `HttpChatClient` ou mantendo-os concentrados no seam) | `LocalChatClient`, `HttpChatClient`, `create_chat_client(settings)`, `ChatClientError` / `ApiClientError` | `LocalChatClient` retornando `ChatResponse` uniforme com `thread_id` e `langfuse_enabled`, `mode_label`, delegação de `reset_conversation` para `reset_thread`, ownership de `close()` (criado vs. injetado) e context manager; `HttpChatClient` (contrato HTTP, timeout, atualização de `langfuse_enabled` via `/health` e `ChatResponse`, erros convertidos em `ApiClientError` subclasse de `ChatClientError` sem PII/URL, `reset_conversation`/`close` no-op); `create_chat_client` selecionando HTTP (com probe `/health` e sem instanciar `ConversationRuntime`) vs. local | Fluxo de widgets do Streamlit |
| **3. UI Streamlit — modo direto** (`tests/test_ui.py`) | `AppTest` cruzando `create_chat_client` / `ConversationRuntime.from_settings` | Fluxo completo de Atendimento, Autenticação, Telemetria de Turno na sidebar (`"Direto (em memória)"`), encerramento, Confirmação de Aumento (confirmar/cancelar/nova conversa), guardrail, erro de provedor, erro amigável de inicialização (`Settings` e `from_settings`), **reutilização** do mesmo `st.session_state.client` em *Nova conversa* com limpeza da thread anterior e `flush()` no Langfuse em cliques repetidos — **sem** acessar `app.session_state.runtime` | Regras internas do grafo e transporte HTTP |
| **4. UI Streamlit — modo HTTP** (`tests/test_ui_http_mode.py`) | `AppTest` cruzando `create_chat_client` com `HttpChatClient` + `httpx.MockTransport` | Turnos HTTP, atualização dinâmica de `thread_id` e `langfuse_enabled` a partir de `/health` e `ChatResponse`, legenda `"API HTTP"`, preservação da Confirmação de Aumento em `ChatClientError`/`ApiClientError`, log neutro sem PII/URL (`"Falha ao enviar turno (ApiClientError)"`), reutilização de `st.session_state.client` em *Nova conversa* sem instanciar `ConversationRuntime` | Detalhes internos de `LocalChatClient` |

### Estratégia *Replace, Don't Layer* nos Testes Existentes

- **`tests/test_runtime.py`**: adicionar testes para `reset_thread` cobrindo limpeza de checkpoint no `InMemorySaver`, `flush()` repetido com `owns_observability=True`, no-op de `flush()` com `owns_observability=False` e idempotência em thread vazia.
- **`tests/test_ui_client.py`** (novo): concentrar os testes de contrato do seam `ChatClient` (`LocalChatClient`, `HttpChatClient`, `create_chat_client` e hierarquia `ApiClientError(ChatClientError)`), movendo os dois testes unitários puros de `HttpChatClient` que hoje estão em `tests/test_ui_http_mode.py` para manter um único ponto de verdade do seam.
- **`tests/test_ui.py`**: remover `app.session_state.runtime.get_state(first_thread)` e referências a `app.session_state.runtime`; verificar a transição para `credito` e o comportamento autenticado pela UI/sidebar (e, para o teste de limpeza de memória + `flush()` em *Nova conversa*, injetar via seam um `LocalChatClient` envolvendo um `ConversationRuntime` observado pelo teste, comprovando que `app.session_state.client is previous`, `runtime.get_state(first_thread) == {}` e `langfuse_client.flush` é chamado em cada *Nova conversa*). Atualizar o target de erro de inicialização de `"banco_agil.ui.app.Settings"` para `"banco_agil.ui.client.Settings"` (ou `create_chat_client`).
- **`tests/test_ui_http_mode.py`**: remover monkeypatch em `banco_agil.ui.app.HttpChatClient` (substituindo por injeção no seam `create_chat_client` ou `banco_agil.ui.client.HttpChatClient`), atualizar a asserção do log de erro para `"Falha ao enviar turno (ApiClientError)"` e confirmar que `st.session_state.client` é reutilizado em *Nova conversa* sem disparar novo `GET /health` nem instanciar `ConversationRuntime`.

## Sequência de Fatias Verticais Sugerida (`/tdd`: red → green por fatia)

1. **Fatia 1 — `ConversationRuntime.reset_thread`:**
   - Red → green em `tests/test_runtime.py` → implementar `reset_thread(thread_id)` em `src/banco_agil/runtime.py`.
2. **Fatia 2 — Seam `ChatClient`, `LocalChatClient`, `HttpChatClient` e `create_chat_client`:**
   - Red → green em `tests/test_ui_client.py` → implementar `ChatClientError`, `ApiClientError(ChatClientError)`, `ChatClient`, `LocalChatClient`, evolução de `HttpChatClient` e `create_chat_client` em `src/banco_agil/ui/client.py`.
3. **Fatia 3 — Migração da UI Streamlit (`src/banco_agil/ui/app.py`):**
   - Atualizar `tests/test_ui.py` e `tests/test_ui_http_mode.py` cruzando o seam `ChatClient` → migrar `src/banco_agil/ui/app.py` para apresentação pura com 0 ramificações de modo e reutilização do `client` em *Nova conversa*.
4. **Fatia 4 — Atualização do ADR 002:**
   - Registrar a nota de evolução (F13) em `docs/adr/002-camada-llm-litellm-e-walking-skeleton.md`.
5. **Gate final no worktree:** rodar `make check` e `make evals` (100% verdes).

## Critérios de Aceite

- [ ] `ConversationRuntime.reset_thread(thread_id)` limpa os checkpoints da thread no checkpointer em memória e dispara `flush()` quando `owns_observability=True`, sem encerrar o ownership.
- [ ] `src/banco_agil/ui/client.py` define `ChatClient` (`Protocol`), `ChatClientError`, `ApiClientError(ChatClientError)`, `HttpChatClient`, `LocalChatClient` (com `owns_runtime` e `from_settings`) e `create_chat_client(settings)`.
- [ ] `HttpChatClient` e `LocalChatClient` retornam `ChatResponse` uniforme e expõem `mode_label`, `langfuse_enabled`, `send_message`, `resume`, `reset_conversation` e `close`.
- [ ] `src/banco_agil/ui/app.py` possui zero condicionais de modo HTTP vs. direto, zero `isinstance(result, ChatResponse)`, zero imports de `ConversationRuntime`/`HttpChatClient`/`ApiClientError`/`Settings` e armazena apenas `st.session_state.client`.
- [ ] Clicar em **Nova conversa** reutiliza o mesmo `st.session_state.client` (sem reconstruir LLM/MCP/Langfuse/grafo), limpa a thread anterior no `InMemorySaver` e chama `flush()` no Langfuse (verificado por teste de regressão).
- [ ] Falha de comunicação (`ChatClientError`/`ApiClientError`) na UI preserva `pending_confirmation` para nova tentativa e registra log neutro sem PII nem URL.
- [ ] `docs/adr/002-camada-llm-litellm-e-walking-skeleton.md` registra explicitamente a atualização da F13 sobre *Nova conversa*.
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`, cobertura geral mantida ≥ 98%).
- [ ] `make evals` 100% verde (12/12 cenários, 0 vazamentos de PII).
- [ ] Commits pequenos em Conventional Commits na branch `feat/f13-chat-client-seam`, sem editar `docs/backlog.md`, `docs/spec.md`, `AGENTS.md` nem commitar arquivos de tooling local.

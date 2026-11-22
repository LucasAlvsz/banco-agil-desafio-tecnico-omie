# Backlog Kanban — Banco Ágil

## Doing
*(vazio)*

## To Do
- [ ] **F13 — Seam `ChatClient` na UI Streamlit:** unificar os modos HTTP e direto atrás do seam `ChatClient` (`HttpChatClient` + `LocalChatClient` envolvendo `ConversationRuntime`) e avaliar atualização do ADR 002 sobre *Nova conversa*.
- [ ] **F14 — Contratos de Agentes e Seam de Guardrails:** contrato HITL tipado, `evaluate_user_input` retornando texto limpo, declarações únicas de `AGENT_NODES`/`HANDOFF_TOOLS`, deduplicação do gate de autenticação em `tools.py` e remoção do import `McpExchangeRateProvider` de `agents/graph.py`.
- [ ] **F15 — Colocalização do Protocolo MCP de Câmbio:** colocalizar cliente e servidor sob `src/banco_agil/mcp/` atrás de `ExchangeRateProvider`, mantendo constantes/codec privados e preservando o ADR 009.
- [ ] **Spike — Helpers de Privacidade/CSV em `evals/runner.py` (Candidato 6):** avaliar consolidação ou simplificação dos helpers auxiliares de privacidade e leitura CSV no runner de evals.

## Done
- [x] **F00 — Fundação:** repo git, `uv`, `ruff`, `mypy`, `pytest`, `pre-commit`, `AGENTS.md`, `docs/spec.md`, `docs/backlog.md`, template de ADR, plano e notas da F00.
- [x] **F01 — Domínio e Persistência CSV:** seeds calibrados, modelos Pydantic v2 imutáveis, normalizadores, fórmula de score, degraus de limite, `Protocol`s, `CreditService`, repositórios CSV atômicos, 95 testes (100% cobertura) e ADR 001.
- [x] **F02 — Walking Skeleton:** `Settings` tipado (`SecretStr`), `llm_factory` (LiteLLM/Groq), grafo mínimo LangGraph (`ToolNode` + `Command` + `InMemorySaver`), chat Streamlit (`make ui`), 137 testes, smoke real na Groq e ADR 002.
- [x] **Spike — Topologia Multiagente LangGraph:** análise dos padrões oficiais do LangGraph v1 e decisão registrada no ADR 003 (4 nós explícitos + `ToolNode` isolado por agente + roteador determinístico + `ToolRuntime`).
- [x] **F03 — Agente de Triagem & Gate de Autenticação:** topologia ADR 003 (`triagem`/`credito`/`cambio` isolados), `ToolRuntime`, gate em 2 camadas (`has_authenticated_client`), 3 tentativas determinísticas, proteção em lotes paralelos, redação precisa de PII, 159 testes (99% cobertura).
- [x] **F04 — Agente de Crédito & HITL:** consulta de limite, `solicitar_aumento_limite` com `interrupt()`/`Command(resume=...)` idempotente, UI Streamlit com botões de confirmação/cancelamento, handoff para `entrevista`, 188 testes (99% cobertura) e ADR 004.
- [x] **F05 — Agente de Entrevista de Crédito:** coleta dos 5 indicadores, `calcular_e_atualizar_score` (`ToolRuntime`, tipos estritos), atualização em `clientes.csv`, handoff invisível de volta ao Crédito e jornada completa ponta a ponta testada (209 testes, 99% cobertura).
- [x] **F06 — Agente de Câmbio:** `ExchangeQuote`, `ExchangeRateProvider`, `normalize_currency_code`, `ExchangeRateClient` (`httpx.AsyncClient`, AwesomeAPI + fallback Frankfurter/BCE), `consultar_cotacao` com ponte sync→async, 274 testes (99% cobertura) e ADR 005.
- [x] **F07 — Guardrails, Robustez e LGPD:** `privacy.py` (`mask_pii`, `mask_pii_data`, `PiiMaskingFilter`, `configure_logging`), `guardrails.py` (limites de entrada, bloqueio determinístico de *prompt injection*, `sanitize_reply`), `DEFAULT_RECURSION_LIMIT=16` com recuperação limpa de `GraphRecursionError`, escopo estrito nos prompts, 348 testes (99% cobertura) e ADR 006.
- [x] **F08 — Evals & Observabilidade:** telemetria local por turno (`TurnTelemetry`, `TurnTelemetryCallback`, `litellm.cost_per_token`), painel na sidebar da UI, Langfuse v4 opcional com `mask` + `mask_otel_spans`, suíte declarativa `evals/scenarios.json` (`make evals`, 12/12 cenários, 0 vazamentos de PII), 366 testes (99% cobertura) e ADR 007.
- [x] **F09 — FastAPI & Docker:** API REST assíncrona (`/health`, `/api/v1/chat`, `lifespan`, `asyncio.to_thread`, lock por `thread_id`, `CreditService._write_lock`, `StrictBool` HITL, handlers 422/500 sem PII), Streamlit híbrido (`API_BASE_URL` / `HttpChatClient`), `Dockerfile` multi-stage (`uv:0.10`, não-root) + `docker-compose.yml`, 406 testes (99% cobertura) e ADR 008.
- [x] **F10 — MCP (Câmbio):** servidor MCP stateless `MCPServer` (`mcp` v2.3.0, `stdio`/`streamable-http` com `TransportSecuritySettings`), cliente `McpExchangeRateProvider` via `mcp.client.Client` (`inmemory`/`stdio`/`streamable_http`), isolamento físico sem `./data` no serviço `mcp-exchange` do Compose, 438 testes (99% cobertura) e ADR 009.
- [x] **F11 — README Final & Guia de Escala:** `README.md` consolidado com visão geral, 2 diagramas Mermaid, funcionalidades, problemas técnicos e soluções, tabela de ADRs `001`–`009`, tutorial de execução/testes (local, API, MCP e Docker Compose) e guia de evolução para produção na AWS (20/20 links válidos).
- [x] **F12 — Runtime do Atendimento e Seam de Observabilidade:** `TurnObservability`/`TurnScope` sem ciclo `observability → agents`, `LangfuseObservability` sem `_handler`/`Lock`, `ConversationRuntime` (`src/banco_agil/runtime.py`) como composition root síncrono com ownership de `flush()`, migração de API (`ApiRuntimeState`), UI (fix `flush()` em *Nova conversa*) e Evals (fix handlers independentes por turno/cenário), `GLOSSARY.md`, 443 testes (99% cobertura) e 12/12 evals.

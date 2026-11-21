# Backlog Kanban — Banco Ágil

## Doing
*(vazio)*

## To Do
- [ ] **F07 — Guardrails, Robustez e LGPD:** tratamento controlado de erros, bloqueio de fora de escopo, filtro de mascaramento de PII em logs.
- [ ] **F08 — Evals & Observabilidade:** dataset de cenários de avaliação (`make evals`), telemetria local (latência/tokens/custo/rota) e integração opcional com Langfuse.
- [ ] **F09 — FastAPI & Docker:** API REST assíncrona (`/api/v1/chat`, `/health`), Streamlit em modo híbrido (direto ou via HTTP) e `Dockerfile`/`docker-compose.yml`.
- [ ] **F10 — MCP (Câmbio):** servidor `FastMCP` de câmbio consumido pelo Agente de Câmbio via `langchain-mcp-adapters` + ADR de fronteira de segurança MCP vs. `InjectedState`.
- [ ] **F11 — README Final & Guia de Escala:** consolidar documentação completa do sistema, diagramas de arquitetura, tutorial de execução/testes e caminhos de evolução para escala.

## Done
- [x] **F00 — Fundação:** repo git, `uv`, `ruff`, `mypy`, `pytest`, `pre-commit`, `AGENTS.md`, `docs/spec.md`, `docs/backlog.md`, template de ADR, plano e notas da F00.
- [x] **F01 — Domínio e Persistência CSV:** seeds calibrados, modelos Pydantic v2 imutáveis, normalizadores, fórmula de score, degraus de limite, `Protocol`s, `CreditService`, repositórios CSV atômicos, 95 testes (100% cobertura) e ADR 001.
- [x] **F02 — Walking Skeleton:** `Settings` tipado (`SecretStr`), `llm_factory` (LiteLLM/Groq), grafo mínimo LangGraph (`ToolNode` + `Command` + `InMemorySaver`), chat Streamlit (`make ui`), 137 testes, smoke real na Groq e ADR 002.
- [x] **Spike — Topologia Multiagente LangGraph:** análise dos padrões oficiais do LangGraph v1 e decisão registrada no ADR 003 (4 nós explícitos + `ToolNode` isolado por agente + roteador determinístico + `ToolRuntime`).
- [x] **F03 — Agente de Triagem & Gate de Autenticação:** topologia ADR 003 (`triagem`/`credito`/`cambio` isolados), `ToolRuntime`, gate em 2 camadas (`has_authenticated_client`), 3 tentativas determinísticas, proteção em lotes paralelos, redação precisa de PII, 159 testes (99% cobertura).
- [x] **F04 — Agente de Crédito & HITL:** consulta de limite, `solicitar_aumento_limite` com `interrupt()`/`Command(resume=...)` idempotente, UI Streamlit com botões de confirmação/cancelamento, handoff para `entrevista`, 188 testes (99% cobertura) e ADR 004.
- [x] **F05 — Agente de Entrevista de Crédito:** coleta dos 5 indicadores, `calcular_e_atualizar_score` (`ToolRuntime`, tipos estritos), atualização em `clientes.csv`, handoff invisível de volta ao Crédito e jornada completa ponta a ponta testada (209 testes, 99% cobertura).
- [x] **F06 — Agente de Câmbio:** `ExchangeQuote`, `ExchangeRateProvider`, `normalize_currency_code`, `ExchangeRateClient` (`httpx.AsyncClient`, AwesomeAPI + fallback Frankfurter/BCE), `consultar_cotacao` com ponte sync→async, 274 testes (99% cobertura) e ADR 005.

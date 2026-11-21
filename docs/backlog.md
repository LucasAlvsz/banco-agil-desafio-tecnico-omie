# Backlog Kanban — Banco Ágil

## Doing
- [ ] **Spike — Topologia Multiagente LangGraph:** investigar padrões oficiais do LangGraph v1 (Híbrido vs. Supervisor vs. Swarm) e registrar ADR de topologia.

## To Do
- [ ] **F03 — Agente de Triagem & Gate de Autenticação:** autenticação contra `clientes.csv`, limite determinístico de 3 falhas consecutivas, roteamento pós-auth e `InjectedState`.
- [ ] **F04 — Agente de Crédito & HITL:** consulta de limite, validação de aumento, `interrupt()` de confirmação antes de gravar CSV + ADR de pontos HITL.
- [ ] **F05 — Agente de Entrevista de Crédito:** coleta das 5 variáveis, recálculo determinístico, atualização em `clientes.csv` e retorno contextual ao Crédito.
- [ ] **F06 — Agente de Câmbio:** cliente HTTP assíncrono (`AwesomeAPI` + fallback `Frankfurter`), encerramento de tópico + ADR de integração de câmbio.
- [ ] **F07 — Guardrails, Robustez e LGPD:** tratamento controlado de erros, bloqueio de fora de escopo, filtro de mascaramento de PII em logs.
- [ ] **F08 — Evals & Observabilidade:** dataset de cenários de avaliação (`make evals`), telemetria local (latência/tokens/custo/rota) e integração opcional com Langfuse.
- [ ] **F09 — FastAPI & Docker:** API REST assíncrona (`/api/v1/chat`, `/health`), Streamlit em modo híbrido (direto ou via HTTP) e `Dockerfile`/`docker-compose.yml`.
- [ ] **F10 — MCP (Câmbio):** servidor `FastMCP` de câmbio consumido pelo Agente de Câmbio via `langchain-mcp-adapters` + ADR de fronteira de segurança MCP vs. `InjectedState`.
- [ ] **F11 — README Final & Guia de Escala:** consolidar documentação completa do sistema, diagramas de arquitetura, tutorial de execução/testes e caminhos de evolução para escala.

## Done
- [x] **F00 — Fundação:** repo git, `uv`, `ruff`, `mypy`, `pytest`, `pre-commit`, `AGENTS.md`, `docs/spec.md`, `docs/backlog.md`, template de ADR, plano e notas da F00.
- [x] **F01 — Domínio e Persistência CSV:** seeds calibrados, modelos Pydantic v2 imutáveis, normalizadores, fórmula de score, degraus de limite, `Protocol`s, `CreditService`, repositórios CSV atômicos, 95 testes (100% cobertura) e ADR 001.
- [x] **F02 — Walking Skeleton:** `Settings` tipado (`SecretStr`), `llm_factory` (LiteLLM/Groq), grafo mínimo LangGraph (`ToolNode` + `Command` + `InMemorySaver`), chat Streamlit (`make ui`), 137 testes, smoke real na Groq e ADR 002.

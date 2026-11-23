# Plano — F11: README Final & Guia de Escala

## Objetivo
Consolidar o `README.md` completo do **Banco Ágil** como documentação principal do repositório, fiel ao estado real do código e dos ADRs `001`–`009`, cobrindo visão geral, arquitetura com diagramas Mermaid, funcionalidades, problemas técnicos enfrentados e soluções, justificativas técnicas, tutorial completo de execução/testes (local e Docker Compose) e guia de evolução para produção em escala (incluindo arquitetura de referência na AWS).

## Dependências (`pyproject.toml`)
- Nenhuma dependência nova (feature puramente de documentação e validação de consistência).

## Escopo

### 1. Consolidação do `README.md` (raiz do repositório)
Estruturar o `README.md` em português, objetivo e escaneável, com as seguintes seções principais:

1. **Visão Geral do Projeto**
   - Propósito do Banco Ágil: atendimento bancário digital multiagente com experiência contínua de persona única.
   - Princípio arquitetural central: regras de negócio, validações críticas e persistência em código determinístico; LLM na camada conversacional e acionamento de ferramentas.
   - Resumo da stack (Python 3.12, LangGraph v1, LiteLLM, Pydantic v2, FastAPI, Streamlit, MCP SDK v2, Langfuse v4 opcional, Docker) e indicadores verificáveis de qualidade (438 testes determinísticos, 99% de cobertura, `mypy --strict`, 12/12 cenários de evals com 0 vazamentos de PII).

2. **Arquitetura do Sistema**
   - **Diagrama Mermaid 1 — Topologia Multiagente (LangGraph):** `START` → roteador determinístico (`_route_agent`) → 4 nós explícitos (`triagem`, `credito`, `entrevista`, `cambio`) e seus `ToolNode`s fisicamente isolados, gate de autenticação, pausa HITL `interrupt()` / `Command(resume=...)` e retorno após entrevista.
   - **Diagrama Mermaid 2 — Serviços e Fronteira de Segurança:** `Streamlit UI` (modo direto ou cliente HTTP) ↔ `FastAPI` (`api`, checkpointer, `CreditService` + `./data`) ↔ `MCPServer` (`mcp-exchange`, stateless, sem `./data`, sem PII) ↔ `AwesomeAPI` + fallback `Frankfurter (BCE)`, `LiteLLM` e `Langfuse v4` opcional (mascaramento LGPD).
   - **Agentes e responsabilidades:** tabela resumindo os 4 agentes, suas ferramentas isoladas e regras de transição.
   - **Manipulação e contratos de dados (`data/`):** `clientes.csv` (fonte da verdade com escrita atômica `fsync` + `os.replace`), `score_limite.csv` (degraus inclusivos), `solicitacoes_aumento_limite.csv` (append-only) e classificação de PII (LGPD).
   - **Estrutura do repositório:** árvore fiel aos diretórios e módulos reais (`src/banco_agil/`, `data/`, `evals/`, `tests/`, `docs/`).

3. **Funcionalidades Implementadas**
   - Autenticação determinística (3 tentativas, entradas mal formatadas não consomem contador, prevenção de enumeração).
   - Consulta de limite e aumento com **Human-in-the-Loop (HITL)** via `interrupt()` / `Command(resume=...)` e `StrictBool`.
   - Entrevista de crédito (5 indicadores, fórmula determinística clampada `[0..1000]`, atualização em `clientes.csv` e preservação do valor desejado para reenvio).
   - Consulta de câmbio via servidor **MCP stateless** (`MCPServer` + `mcp.client.Client`, `inmemory`/`stdio`/`streamable_http`) com AwesomeAPI e fallback Frankfurter (BCE) preservando a data de referência.
   - Guardrails, robustez e LGPD (limite de 2.000 caracteres, bloqueio determinístico de *prompt injection*, `recursion_limit=16`, escopo estrito nos prompts, `PiiMaskingFilter` global e redação unificada de CPF/data de nascimento/segredos).
   - Observabilidade em duas camadas (telemetria local por turno na sidebar da UI + Langfuse v4 opcional com `mask` e `mask_otel_spans`) e suíte declarativa de evals (`make evals`, 12 cenários offline + modo `live`).
   - API REST assíncrona FastAPI (`/health`, `/api/v1/chat`), Streamlit híbrido e Docker Compose com 3 serviços (`mcp-exchange`, `api`, `ui`).

4. **Problemas Técnicos Enfrentados e Como Foram Resolvidos**
   - Lotes paralelos de ferramentas e prioridade de encerramento (`wrap_tool_call` / `_guard_tool_call`).
   - Redação LGPD de datas vs. preservação da data de referência de câmbio (BCE) usando `preserve_dates_except`.
   - Orçamento de recursão do LangGraph v1 (`10.007` padrão) e lotes interrompidos em orçamento ímpar ou pós-retomada HITL (`DEFAULT_RECURSION_LIMIT = 16` + remoção cirúrgica via `RemoveMessage`).
   - Concorrência na API FastAPI (cancelamento de requisição HTTP com `asyncio.shield` + `add_done_callback`, `CreditService._write_lock` para CSVs e `CallbackHandler` por requisição no Langfuse).
   - Incompatibilidade upstream de `langchain-mcp-adapters` 0.3.x com `mcp` v2, proteção contra DNS rebinding (`TransportSecuritySettings` no Docker Compose) e isolamento físico de `data/` fora da imagem Docker compartilhada.
   - Determinismo total em testes (bloqueio de sockets e `subprocess.Popen` em `tests/conftest.py`, `httpx.MockTransport`, `ASGITransport`, `AppTest` e `mcp.client.Client(server)` em memória).

5. **Escolhas Técnicas e Justificativas**
   - Tabela consolidada referenciando os **9 ADRs** ([docs/adr/001](../../adr/001-dominio-e-persistencia-csv.md) a [docs/adr/009](../../adr/009-servidor-mcp-cambio-e-fronteira-de-seguranca.md)) com decisão adotada, alternativa descartada e motivação.

6. **Tutorial de Execução e Testes**
   - Pré-requisitos (`Python 3.12+`, `uv`, Docker opcional).
   - Configuração inicial (`make setup`, `.env` a partir de `.env.example`, provedores suportados pelo LiteLLM e variáveis opcionais de Langfuse/HTTP/MCP).
   - Modos de execução:
     - Interface Streamlit direta em memória (`make ui`).
     - API REST FastAPI (`make api`) com exemplos `curl` para `GET /health` e `POST /api/v1/chat` (turno inicial e retomada HITL `"approve": true`).
     - Streamlit em modo cliente HTTP (`API_BASE_URL=http://localhost:8000 make ui`).
     - Servidor MCP de câmbio standalone (`make mcp-exchange`).
     - Stack completa via Docker Compose (`docker compose up --build`, destacando a porta da UI `8501`, da API `8000`, isolamento de `mcp-exchange` e necessidade de montar `./data:/app/data` em `docker run` avulso).
   - **Roteiro prático de testes manuais** com os clientes calibrados de `data/clientes.csv` (Ana Souza para fluxo rejeitado → entrevista → aprovado; Carlos Mendes para aprovação direta; testes de câmbio, fora de escopo, *prompt injection* e encerramento).
   - **Testes automatizados, qualidade e evals:** `make check`, `make test`, `make evals`, saída `--json` e execução opcional em modo `live` (`uv run python -m banco_agil.evals --mode live`).

7. **Evolução e Escala (Guia para Produção)**
   - Tabela completa conectando cada limite intencional do MVP ao caminho de evolução em produção (arquitetura de referência na AWS):
     - Computação e exposição: ECS Fargate / EKS atrás de ALB + API Gateway (múltiplas réplicas stateless, superando `--workers 1`), SSE/WebSocket.
     - Memória de sessão e HITL assíncrono: checkpointer durável (PostgreSQL/Aurora ou DynamoDB) + revisão de backoffice via SQS e retomada por webhook/worker.
     - Persistência transacional: PostgreSQL/RDS atrás dos `Protocol`s existentes com transação única e trilha de auditoria.
     - Gateway de LLM e Guardrails: LiteLLM Proxy / Amazon Bedrock + Amazon Bedrock Guardrails.
     - Segurança e MCP: OAuth 2.1/JWT, WAF + Redis para rate limiting distribuído por CPF/IP, MCP Auth com escopos granulares + mTLS, ElastiCache Redis (TTL 30–60s) e circuit breaker no câmbio.
     - Observabilidade e Evals contínuos: OpenTelemetry Collector com DLP, `sample_rate`, alertas de SLO (p95, custo/sessão, falhas/bloqueios) e evals `live` agendados em staging.

### 2. Regras de Qualidade e Consistência da Documentação
- Verificar que todos os links relativos no `README.md` (`docs/spec.md`, `docs/adr/001..009`, `.env.example`, `evals/scenarios.json`, etc.) existem exatamente no repositório.
- Verificar que todos os comandos, portas, variáveis de ambiente, números de testes/cobertura e nomes de arquivos no `README.md` correspondem exatamente ao estado atual do código.
- Validar que os blocos Mermaid usam apenas sintaxe suportada (`flowchart TD` / `flowchart LR`, rótulos com parênteses/colchetes entre aspas, sem HTML tags).
- Tratar sempre o Banco Ágil como produto MVP real, sem termos internos ou de processo.

## Fora de Escopo
- Alterações de código em `src/` ou `tests/`.
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Verificação
- Conferir links relativos do `README.md` apontando para arquivos existentes no repositório.
- Rodar `make check` e `make evals` garantindo que nada foi quebrado (incluindo build do pacote via hatchling que lê `README.md`).
- Rodar `docker compose config --quiet` confirmando integridade da configuração documentada.

## Critérios de Aceite
- [ ] `README.md` completo com todas as 7 seções, 2 diagramas Mermaid válidos, tabela de ADRs `001`–`009`, exemplos reais de execução/testes e guia de escala.
- [ ] Todos os links relativos do `README.md` validados contra arquivos reais do repositório.
- [ ] `make check` e `make evals` 100% verdes.
- [ ] Commits limpos em Conventional Commits na branch `feat/f11-readme-scale` (incluindo `plan.md`).

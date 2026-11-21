# Plano — F08: Evals & Observabilidade

## Objetivo
Implementar **observabilidade em duas camadas** (telemetria local sempre ativa por turno exibida na UI + integração opcional ao **Langfuse v4** via `.env` com mascaramento LGPD) e uma **suíte automatizada de avaliação (`make evals`)** baseada em dataset declarativo cobrindo os fluxos críticos de ponta a ponta (autenticação, crédito com HITL, entrevista, câmbio com fallback, guardrails, fora de escopo, LGPD e encerramento global).

## Dependências (`pyproject.toml`)
- Adicionar `langfuse>=4.0.0` em `[project].dependencies` (executar `uv sync` para atualizar `uv.lock`).
- Garantir que o módulo de observabilidade só instancie `Langfuse` quando `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY` estiverem preenchidos — zero conexões de rede ou threads de background quando desativado ou durante o `pytest`.

## Escopo

### 1. Telemetria Local e Integração Opcional Langfuse (`src/banco_agil/infrastructure/observability.py`, `config.py`, `.env.example`)
- **Configuração (`config.py` e `.env.example`):**
  - Adicionar em `Settings`:
    - `langfuse_public_key: SecretStr | None = None`
    - `langfuse_secret_key: SecretStr | None = None`
    - `langfuse_base_url: str | None = None` (padrão efetivo `"https://cloud.langfuse.com"` quando omitido; aceita fallback legado `LANGFUSE_HOST` apenas se `langfuse_base_url` estiver vazio).
  - Documentar `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` e `LANGFUSE_BASE_URL` (comentados/vazios) em `.env.example`.
- **Modelo de telemetria por turno (`TurnTelemetry(BaseModel)`, `frozen=True`, `allow_inf_nan=False`):**
  - `latency_ms: float` (`ge=0.0`)
  - `input_tokens: int = 0` (`ge=0`)
  - `output_tokens: int = 0` (`ge=0`)
  - `total_tokens: int = 0` (`ge=0`)
  - `estimated_cost_usd: float | None = None` (`ge=0.0`)
  - `model_name: str | None = None`
  - `agent_route: tuple[AgentName, ...] = ()`
  - `tools_called: tuple[str, ...] = ()`
  - `guardrail_blocked: bool = False`
  - **Regra LGPD:** `TurnTelemetry` nunca armazena texto do usuário, respostas do modelo, argumentos de ferramentas nem dados pessoais.
- **Estimativa de custo (`estimate_cost_usd(model_name, input_tokens, output_tokens) -> float | None`):**
  - Retorna `0.0` quando `input_tokens == 0 and output_tokens == 0`.
  - Retorna `None` quando `model_name` é `None` ou vazio.
  - Usa `litellm.cost_per_token(model=model_name, prompt_tokens=input_tokens, completion_tokens=output_tokens)` somando entrada e saída (compatível com `LITELLM_LOCAL_MODEL_COST_MAP=True`, zero rede); em modelo desconhecido ou qualquer exceção, retorna `None` sem interromper o fluxo.
- **Callback local de telemetria (`TurnTelemetryCallback(BaseCallbackHandler)`):**
  - `on_chain_start`: captura `metadata.get("langgraph_node")` quando for um dos 4 agentes (`triagem`, `credito`, `entrevista`, `cambio`), registrando a rota sem duplicatas consecutivas (ex.: `("triagem", "credito")`).
  - `on_tool_start`: captura somente o nome da ferramenta (`name` ou `serialized["name"]`), nunca os argumentos/input.
  - `on_llm_end`: acumula `input_tokens`, `output_tokens` e `total_tokens` a partir de `AIMessage.usage_metadata` (padrão LangChain v1) com fallback para `response_metadata["token_usage"]` ou `response.llm_output["token_usage"]`, e detecta `model_name` (com fallback para `default_model_name`).
  - Método `build(latency_ms: float, *, guardrail_blocked: bool = False) -> TurnTelemetry` que calcula `estimated_cost_usd` e retorna o snapshot imutável.
- **Integração opcional Langfuse v4 (`LangfuseObservability` / `create_langfuse_observability(settings: Settings)`):**
  - Verificação `is_langfuse_configured(settings: Settings) -> bool`: verdadeira apenas quando `langfuse_public_key` e `langfuse_secret_key` contêm valores não vazios.
  - Mascaramento LGPD em defesa em profundidade (reutilizando `mask_pii` e `mask_pii_data` de `privacy.py`):
    - `langfuse_mask_data(*, data: Any, **kwargs: Any) -> Any`: delega para `mask_pii_data(data)` (passado em `Langfuse(..., mask=...)`).
    - `langfuse_mask_otel_spans(*, params: Any) -> Any`: percorre `params.spans.items()`, aplica `mask_pii` a atributos `str` (e `mask_pii_data` a coleções de strings) e retorna `MaskOtelSpansResult(span_patches=patches)` (passado em `Langfuse(..., mask_otel_spans=...)`, conforme recomendação oficial da Langfuse Python SDK v4).
  - Quando não configurado: retorna `None` sem importar/instanciar `Langfuse` (zero rede, zero threads).
  - Quando configurado: inicializa `Langfuse(public_key=..., secret_key=..., base_url=..., mask=langfuse_mask_data, mask_otel_spans=langfuse_mask_otel_spans)` e expõe `create_handler() -> BaseCallbackHandler` (`from langfuse.langchain import CallbackHandler; CallbackHandler(public_key=...)`) e `flush() -> None`.
  - Qualquer falha na inicialização ou no `flush()` registra apenas `logger.warning("Langfuse indisponível (%s)", type(exc).__name__)` e degrada silenciosamente para observabilidade local (nunca derruba o atendimento).

### 2. Instrumentação no Grafo e UI (`src/banco_agil/agents/graph.py`, `src/banco_agil/ui/app.py`)
- **`src/banco_agil/agents/graph.py`:**
  - Adicionar campo `telemetry: TurnTelemetry` em `TurnResult` (com default de compatibilidade se construído diretamente, e sempre preenchido por `_execute_turn`).
  - Estender `run_turn` e `resume_turn` com parâmetros opcionais:
    - `callbacks: Sequence[BaseCallbackHandler] = ()`
    - `model_name: str | None = None`
  - Em `_execute_turn`:
    - Medir latência com `time.perf_counter()` desde o início do turno.
    - Instanciar `TurnTelemetryCallback(default_model_name=model_name)` e incluir em `config["callbacks"] = [telemetry_cb, *callbacks]`.
    - Incluir `config["metadata"] = {"langfuse_session_id": thread_id, "langfuse_tags": ["banco-agil"]}` (nunca enviar CPF/nome em `langfuse_user_id`).
    - Em turnos bloqueados pelo guardrail: `guardrail_blocked=True`, `input_tokens=0`, `output_tokens=0`, `total_tokens=0`, `estimated_cost_usd=0.0`, `agent_route=(active_agent,)`.
    - Em turnos encerrados, confirmação pendente ou erro: preencher `TurnTelemetry` com a latência medida e os dados coletados até o ponto da interrupção/erro.
- **`src/banco_agil/ui/app.py`:**
  - Guardar `st.session_state.last_telemetry: TurnTelemetry | None`, `st.session_state.active_agent: AgentName` e `st.session_state.llm_model: str` (limpando em **Nova conversa**).
  - Inicializar `langfuse_observability = create_langfuse_observability(settings)` na criação da sessão (armazenando no `session_state`) e passar `callbacks` + `model_name` para `run_turn`/`resume_turn` (com `flush()` seguro quando ativo? Como o Streamlit mantém o processo vivo, basta passar o `CallbackHandler` por turno; o flush é gerenciado em background pelo SDK).
  - Exibir na sidebar (`st.sidebar`) o painel **"Telemetria do turno"** com:
    - Agente ativo e rota do turno (ex.: `triagem → credito`)
    - Latência (`ms`)
    - Tokens (`entrada`, `saída`, `total`)
    - Custo estimado em USD (ou `N/D` quando indisponível)
    - Status da observabilidade externa (`Langfuse ativo` ou `Local (Langfuse desativado)`)
    - Zero PII exibida na sidebar.

### 3. Suíte de Evals Automatizados (`evals/scenarios.json`, `src/banco_agil/evals/`, `Makefile`)
- **Dataset declarativo (`evals/scenarios.json`):**
  - Cobrir pelo menos **12 cenários** representando as jornadas e invariantes críticos do produto (usando os dados calibrados de `data/clientes.csv` e `data/score_limite.csv`):
    1. `auth_and_credit_limit_query`: autenticação (Ana Souza) → `credito` → consulta de limite (`R$ 2500.00`).
    2. `auth_invalid_format_recovery`: CPF/data mal formatados não consomem tentativas e permitem autenticação válida na sequência.
    3. `auth_three_failures_lockout`: 3 falhas consecutivas encerram sessão com mensagem genérica.
    4. `credit_increase_approved_hitl`: Carlos Mendes solicita `R$ 10000.00` → pausa HITL → confirmação → `aprovado` e `clientes.csv` atualizado.
    5. `credit_increase_canceled_hitl`: pausa HITL → cancelamento → CSVs preservados.
    6. `credit_rejected_interview_resubmission`: Ana Souza pede `R$ 6000.00` → `rejeitado` → entrevista (5 indicadores, score `590`) → retorno ao `credito` → reenvio confirmado → `aprovado` (`2` pedidos no CSV, limite `6000.00`).
    7. `exchange_quote_primary`: autenticação → `cambio` → cotação do dólar via AwesomeAPI → retorno à `triagem` mantendo autenticação.
    8. `exchange_quote_fallback_bce`: falha da AwesomeAPI → fallback Frankfurter/BCE preservando a data de referência (`2026-11-20`) enquanto redige PII.
    9. `guardrail_prompt_injection`: tentativa de *prompt injection* bloqueada determinísticamente (`guardrail_blocked=True`, `total_tokens=0`, sem chamada ao LLM).
    10. `out_of_scope_refusal`: pedido fora de escopo (Pix) recusado educadamente mantendo `active_agent="triagem"` e sessão aberta.
    11. `lgpd_pii_and_secret_redaction`: resposta do modelo contendo CPF, nascimento e chave `gsk_...` sai totalmente mascarada em `TurnResult.reply`.
    12. `global_closure_from_specialist`: encerramento solicitado durante atendimento especialista encerra a sessão e bloqueia novos turnos.
- **Runner e modelos (`src/banco_agil/evals/`):**
  - `models.py`: modelos Pydantic (`frozen=True`) para o dataset (`EvalScenario`, `EvalTurn`, `EvalExpectations`), resultado por cenário (`ScenarioEvalResult`) e relatório consolidado (`EvalReport` com `total_scenarios`, `passed_scenarios`, `failed_scenarios`, `pass_rate`, `route_accuracy`, `pii_leaks`, `avg_latency_ms`, `p95_latency_ms`, `total_tokens`, `total_cost_usd`).
  - `runner.py`:
    - `load_scenarios(path: Path = DEFAULT_SCENARIOS_PATH) -> list[EvalScenario]`
    - `run_evals(*, scenarios_path: Path = ..., mode: Literal["offline", "live"] = "offline", settings: Settings | None = None) -> EvalReport`
    - **Modo `offline` (padrão, determinístico, zero rede):** executa cada cenário em diretório temporário isolado (cópia de `data/`), usando grafo real, `CreditService` real, cliente/provedor de câmbio determinístico (incluindo cenário de fallback HTTP via `httpx.MockTransport`) e `EvalScriptedChatModel` (emite `AIMessage` com `usage_metadata` realista para validar telemetria e custo).
    - **Modo `live` (opcional, manual):** executa os cenários contra `create_chat_model(settings)` (e opcionalmente `create_langfuse_observability(settings)` com `flush()` ao final), validando as mesmas asserções estruturais de estado/CSV/zero-PII sem depender das respostas scripted.
    - Checagem automática de LGPD em **todos os cenários**: nenhuma resposta final (`TurnResult.reply`) pode conter CPF ou data de nascimento dos seeds (`pii_leaks == 0`).
  - `__main__.py`: CLI enxuta (`python -m banco_agil.evals [--mode offline|live] [--dataset PATH] [--json]`), imprimindo resumo legível em tabela (ou JSON com `--json`) e saindo com código `0` se todos os cenários passarem ou `1` caso contrário.
- **`Makefile`:**
  - Adicionar alvo `evals`: `uv run python -m banco_agil.evals` e registrar `evals` em `.PHONY`.

### 4. Documentação Arquitetural (`docs/adr/007-evals-e-observabilidade.md`)
Seguindo `docs/adr/000-template.md`, registrar:
- Observabilidade em duas camadas (telemetria local sempre ativa via `BaseCallbackHandler` + `litellm.cost_per_token`, somada a Langfuse v4 opcional com `mask` e `mask_otel_spans` reutilizando `privacy.py`).
- Suíte de avaliação declarativa (`evals/scenarios.json` + `make evals` offline determinístico e modo live sob demanda).
- Alternativas descartadas: depender exclusivamente de SaaS externo para telemetria; rodar LLM-as-a-judge no CI determinístico; manter tabela manual de preços de modelos.
- Consequências, trade-offs e **Evolução e Escala**: OpenTelemetry Collector centralizado, pipelines agendados de evals live (regressão de prompts/modelos com avaliadores semânticos complementares), amostragem (`sample_rate`) em alto volume e alertas de SLO (latência p95, custo por atendimento, taxa de falhas/bloqueios).

## Fora de Escopo
- API REST FastAPI e Docker (`F09`).
- Servidor `FastMCP` (`F10`).
- README final consolidado (`F11`).
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes (100% determinísticos, zero rede)
- `tests/test_observability.py`:
  1. `estimate_cost_usd`: retorna `0.0` para 0 tokens, `None` para modelo ausente/desconhecido, e soma `cost_per_token` (mock/local) para modelo conhecido sem rede.
  2. `TurnTelemetryCallback` + `run_turn` / `resume_turn`: captura rota multiagente sem duplicatas consecutivas (`("triagem", "credito")`), ferramentas chamadas (sem args/PII), soma de tokens across múltiplas chamadas de LLM no mesmo turno (tanto via `usage_metadata` quanto fallback `response_metadata["token_usage"]`), latência `>= 0` e `guardrail_blocked=True` com `0` tokens / `0.0` USD.
  3. `langfuse_mask_data` e `langfuse_mask_otel_spans`: mascaram CPF, datas e segredos em dicionários aninhados e em spans OTel preservando atributos sem PII.
  4. `create_langfuse_observability`: retorna `None` sem chaves (sem importar/instanciar `Langfuse` nem abrir rede); com chaves configuradas (com `Langfuse` e `CallbackHandler` fake injetados/monkeypatched), cria client com `mask` e `mask_otel_spans`, repassa `langfuse_session_id=thread_id` nos metadados sem `langfuse_user_id`/PII, e degrada graciosamente com `warning` em erro de inicialização ou `flush()`.
- `tests/test_evals.py`:
  1. `load_scenarios`: carrega e valida integralmente `evals/scenarios.json` (≥ 12 cenários com IDs únicos).
  2. `run_evals(mode="offline")`: executa a suíte completa de `evals/scenarios.json` determinísticamente (zero rede) e confirma `pass_rate == 1.0`, `failed_scenarios == 0`, `route_accuracy == 1.0`, `pii_leaks == 0`, `total_tokens > 0`, e `data/` original intocado.
  3. Falha detectada: cenário artificial com expectativa divergente ou vazamento de PII gera `passed=False`, incrementa `failed_scenarios`/`pii_leaks` e faz a CLI retornar exit code `1`.
  4. CLI (`python -m banco_agil.evals` / função `main`): valida saída tabular e saída `--json` válida com exit code `0`.
  5. Modo `live` (testado com `create_chat_model` monkeypatched para modelo fake offline, garantindo zero rede): valida o caminho de execução live e chamada a `flush()` quando observabilidade está ativa.
- `tests/test_ui.py`:
  1. Verifica exibição da telemetria local na sidebar (`AppTest`) após um turno (agente ativo, latência, tokens, custo e status do Langfuse) e reset ao clicar em **Nova conversa**, sem expor PII na sidebar.

## Critérios de Aceite
- [x] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [x] `make evals` 100% verde (12+ cenários passando, `pass_rate == 100%`, `pii_leaks == 0`).
- [x] Cobertura ≥ 95% nos módulos novos (`infrastructure/observability.py` e `evals/`) e cobertura geral mantida ≥ 98%.
- [x] `docs/adr/007-evals-e-observabilidade.md` criado seguindo `docs/adr/000-template.md`.
- [x] Commits pequenos em Conventional Commits na branch `feat/f08-evals-observability` (incluindo `plan.md`).

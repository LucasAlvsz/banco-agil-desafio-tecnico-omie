# Notas — F14: Contratos de Agentes e Seam de Guardrails

## Entregue
- **Contratos compartilhados e `AGENT_NODES` (`src/banco_agil/agents/state.py`):**
  - Derivado `AGENT_NODES: tuple[AgentName, ...] = get_args(AgentName)` diretamente de `AgentName` (mantendo reexport em `agents/graph.py` para compatibilidade).
  - Movido `LimitIncreaseConfirmation(BaseModel)` para `agents/state.py` (mantendo reexport em `agents/graph.py`) com `ConfigDict(frozen=True, allow_inf_nan=False, json_schema_serialization_defaults_required=True)`, preservando exatamente os 3 campos públicos no schema OpenAPI de `ChatResponse.pending_confirmation` e concentrando o protocolo de pausa em `to_interrupt_payload()` e `from_interrupt_value()`.
  - Introduzido `LimitIncreaseDecision(BaseModel)` (`frozen=True`, `extra="forbid"`, `approved: StrictBool`) com `to_resume_payload()` e `is_approved()` rejeitando tipos não booleanos, chaves extras e dicionários incompletos na retomada da Confirmação de Aumento.
- **Seam de guardrails com entrada higienizada (`src/banco_agil/agents/guardrails.py`):**
  - Adicionado `cleaned_text: str | None = None` em `InputGuardrailDecision` com `@model_validator(mode="after")` impondo invariantes simétricos entre entradas permitidas e bloqueadas.
  - `evaluate_user_input()` retorna `cleaned_text` higienizado quando permitido, mantendo a função pura e eliminando a higienização duplicada em `agents/graph.py`.
- **Gate de Autenticação deduplicado, handoffs declarativos e HITL tipado (`src/banco_agil/agents/tools.py`):**
  - Extraído `_require_authenticated_client(runtime)` substituindo as 5 cópias do gate de Autenticação e eliminando os 3 `cast(str, runtime.state["client_cpf"])`.
  - Atualizada `solicitar_aumento_limite` para usar `LimitIncreaseConfirmation.to_interrupt_payload()` e `LimitIncreaseDecision.is_approved()`.
  - Marcadas as 5 ferramentas de handoff com `@tool(extras={_HANDOFF_EXTRA: True})` e exposto `is_handoff_tool(tool)` sem vazar `extras` para o schema enviado ao LLM.
- **Orquestração limpa sem acoplamento MCP (`src/banco_agil/agents/graph.py`):**
  - Removido o import de `McpExchangeRateProvider` (`banco_agil.infrastructure.mcp_exchange_client`) e tornado `exchange_provider: ExchangeRateProvider` parâmetro obrigatório em `build_graph`.
  - Removido o conjunto literal `HANDOFF_TOOLS`, derivando os handoffs em `build_graph` via `is_handoff_tool` e consumindo `decision.cleaned_text`, `LimitIncreaseConfirmation.from_interrupt_value` e `LimitIncreaseDecision.to_resume_payload` em `_execute_turn` / `resume_turn`.
- **Testes (*replace, don't layer*):**
  - Nova suíte `tests/test_agent_contracts.py` cobrindo `AGENT_NODES`, `LimitIncreaseConfirmation` e `LimitIncreaseDecision`, fixture `exchange_provider` com falha ruidosa em `tests/conftest.py` (incluindo verificação no teardown) e atualização de `tests/test_guardrails.py`, `tests/test_triage_agent.py`, `tests/test_credit_agent.py`, `tests/test_interview_agent.py`, `tests/test_walking_skeleton.py`, `tests/test_observability.py` e `tests/test_api.py`.

## Revisão
- `/code-review` em dois eixos (Standards e Spec) sobre `main...50e65a3`: 0 violações duras de padrão documentado, 3 ajustes de baseline em Standards e 1 asserção parcial em Spec.
- Todos os 4 achados corrigidos em `bdf849b`: despacho único de `turn_input` sem `assert` em produção em `agents/graph.py`, constante `_HANDOFF_EXTRA` e nomeação consistente `auth` antes de `cpf` em `agents/tools.py`, e asserção explícita de ausência de `McpExchangeRateProvider` em `tests/test_walking_skeleton.py`.

## Verificação
- `make check`: `ruff check`, `ruff format --check`, `mypy src` (`--strict`, 35 arquivos) e 475 testes passando (99% cobertura total; 100% em `agents/state.py`, `agents/guardrails.py` e `agents/graph.py`; 99% em `agents/tools.py`).
- `make evals`: 12/12 cenários aprovados (100%), rotas 100%, 0 vazamentos de PII.

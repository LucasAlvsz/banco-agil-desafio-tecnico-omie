# Plano — F14: Contratos de Agentes e Seam de Guardrails

## Objetivo

Aprofundar os contratos internos do pacote `src/banco_agil/agents/` e a costura de guardrails conforme o **Candidato 3** da revisão arquitetural (`architecture-review-20261121-080832.html`), eliminando vazamentos entre `state.py`, `guardrails.py`, `tools.py` e `graph.py` e removendo o acoplamento de `agents/graph.py` à infraestrutura MCP de câmbio.

A mudança resolve cinco dores concretas da arquitetura atual:

1. **Protocolo HITL em dicionários sem tipo:** `tools.py` monta o payload do `interrupt()` e valida `decision.get("approved") is True` manualmente, enquanto `graph.py` serializa `{"approved": approved}` e inspeciona `value.get("action") == "confirm_limit_increase"` separadamente.
2. **Seam raso em `guardrails.py`:** `evaluate_user_input` higieniza bytes nulos e espaços (`replace("\x00", "").strip()`), descarta o texto limpo e obriga `graph.py` a repetir a mesma higienização ao montar `HumanMessage`.
3. **Declarações duplicadas de nós e handoffs:** `AGENT_NODES` em `graph.py` duplica os literais de `AgentName` em `state.py`, e `HANDOFF_TOOLS` em `graph.py` lista 5 strings brutas separadas das definições de ferramentas em `tools.py` (incluindo a closure `calcular_e_atualizar_score`).
4. **Gate de Autenticação copiado 5 vezes em `tools.py`:** `consultar_limite`, `solicitar_aumento_limite`, `calcular_e_atualizar_score`, `consultar_cotacao` e `_redirect` repetem a mesma verificação de `has_authenticated_client(runtime.state)` + retorno para `"triagem"`, acompanhada de 3 `cast(str, runtime.state["client_cpf"])`.
5. **Import de infraestrutura MCP em `agents/graph.py`:** `graph.py` importa `McpExchangeRateProvider` exclusivamente para fornecer um valor padrão quando `exchange_provider` é omitido em `build_graph`, embora `ConversationRuntime.from_settings` já injete `create_exchange_provider(settings)` em todos os fluxos de produção.

## Escopo

### 1. Contratos Compartilhados e `AGENT_NODES` (`src/banco_agil/agents/state.py`)

- **Declaração única de `AGENT_NODES`:**
  - Derivar `AGENT_NODES: tuple[AgentName, ...] = get_args(AgentName)` diretamente de `AgentName` em `agents/state.py` (reexportado em `agents/graph.py` para compatibilidade).
- **Contrato tipado de Confirmação de Aumento (pausa + retomada):**
  - Definir `CONFIRM_LIMIT_INCREASE_ACTION = "confirm_limit_increase"` e `DEFAULT_LIMIT_INCREASE_CONFIRMATION_MESSAGE = "Você confirma o pedido de aumento de limite?"`.
  - Mover `LimitIncreaseConfirmation(BaseModel)` de `agents/graph.py` para `agents/state.py` (mantendo reexport em `agents/graph.py`, sem alterar `ui/app.py` nem `api/schemas.py`):
    - `model_config = ConfigDict(frozen=True, allow_inf_nan=False)`.
    - Preservar exatamente os 3 campos públicos (`limite_atual: float`, `novo_limite_solicitado: float`, `message: str = DEFAULT_LIMIT_INCREASE_CONFIRMATION_MESSAGE`), mantendo intacto o schema OpenAPI de `ChatResponse.pending_confirmation`.
    - `to_interrupt_payload(self) -> dict[str, Any]`: devolve `{"action": CONFIRM_LIMIT_INCREASE_ACTION, **self.model_dump()}`, JSON-serializável conforme [ADR 004](../../adr/004-agente-credito-e-hitl.md).
    - `@classmethod def from_interrupt_value(cls, value: Any) -> Self | None`: retorna `None` se `not isinstance(value, dict) or value.get("action") != CONFIRM_LIMIT_INCREASE_ACTION`; caso contrário, valida e retorna `cls.model_validate(value)`.
  - Introduzir `LimitIncreaseDecision(BaseModel)`:
    - `model_config = ConfigDict(frozen=True, extra="forbid")`.
    - `approved: StrictBool`.
    - `to_resume_payload(self) -> dict[str, bool]`: devolve `{"approved": self.approved}`.
    - `@classmethod def is_approved(cls, value: Any) -> bool`: tenta `cls.model_validate(value)` e retorna `parsed.approved`; em `ValidationError`, retorna `False` (rejeitando inteiros como `1`, strings como `"true"`/`"sim"`, chaves extras ou dicionários incompletos).

### 2. Seam de Guardrails Retornando Entrada Higienizada (`src/banco_agil/agents/guardrails.py`)

- Evoluir `InputGuardrailDecision(BaseModel)`:
  - Adicionar `cleaned_text: str | None = None` (simétrico a `reply: str | None = None`).
  - Garantir os invariantes via `@model_validator(mode="after")`:
    - Se `allowed` for `True`: `reason == "ok"`, `reply is None` e `cleaned_text` é uma `str` não vazia.
    - Se `allowed` for `False`: `reason != "ok"`, `reply` é uma `str` não vazia e `cleaned_text is None`.
- Atualizar `evaluate_user_input(message: str, *, max_chars: int = MAX_USER_MESSAGE_CHARS) -> InputGuardrailDecision`:
  - Ao permitir a entrada, retornar `InputGuardrailDecision(allowed=True, cleaned_text=cleaned)`.
  - Manter a função pura (sem logging colateral; o log de bloqueio permanece em `agents/graph.py`).

### 3. Gate de Autenticação Deduplicado, Marcação de Handoffs e HITL Tipado (`src/banco_agil/agents/tools.py`)

- **Gate de Autenticação único:**
  - Extrair `_require_authenticated_client(runtime: ToolRuntime[None, ConversationState]) -> str | Command[Any]`:
    - Se `not has_authenticated_client(runtime.state)`: retorna `_tool_result(runtime, "Autentique o cliente primeiro.", active_agent="triagem")`.
    - Caso contrário, retorna o `client_cpf` autenticado (`str`), eliminando os 3 `cast(str, runtime.state["client_cpf"])` e as 5 cópias do gate em `consultar_limite`, `solicitar_aumento_limite`, `calcular_e_atualizar_score`, `consultar_cotacao` e `_redirect`.
    - Preservar intacta a inferência de schema do `@tool` do LangChain (sem decorators que alterem a assinatura das ferramentas).
- **Uso do contrato HITL tipado em `solicitar_aumento_limite`:**
  - Construir `LimitIncreaseConfirmation(limite_atual=client.limite_atual, novo_limite_solicitado=novo_limite).to_interrupt_payload()` ao chamar `interrupt(...)`.
  - Validar a resposta com `if not LimitIncreaseDecision.is_approved(decision):`.
- **Declaração única de ferramentas de handoff:**
  - Marcar as 5 ferramentas de handoff diretamente na definição `@tool(extras={"handoff": True})`:
    - `calcular_e_atualizar_score` (dentro de `build_interview_tools`),
    - `redirecionar_para_credito`,
    - `redirecionar_para_entrevista`,
    - `redirecionar_para_cambio`,
    - `voltar_para_triagem`.
  - Expor `is_handoff_tool(tool: BaseTool) -> bool`, retornando `bool((tool.extras or {}).get("handoff"))` (sem vazar `extras` para o schema OpenAI enviado ao LLM).

### 4. Orquestração Limpa em `src/banco_agil/agents/graph.py`

- **Remoção do import de infraestrutura MCP:**
  - Remover `from banco_agil.infrastructure.mcp_exchange_client import McpExchangeRateProvider`.
  - Tornar `exchange_provider: ExchangeRateProvider` parâmetro obrigatório (posicional-ou-keyword) em:
    ```python
    def build_graph(
        model: BaseChatModel,
        credit_service: CreditService,
        exchange_provider: ExchangeRateProvider,
        *,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> ConversationGraph:
    ```
  - O wiring de produção (`create_exchange_provider(settings)`) permanece concentrado em `ConversationRuntime.from_settings` (`src/banco_agil/runtime.py`).
- **Remoção das declarações duplicadas:**
  - Importar `AGENT_NODES`, `LimitIncreaseConfirmation` e `LimitIncreaseDecision` de `banco_agil.agents.state` (mantendo `LimitIncreaseConfirmation` e `AGENT_NODES` reexportados no namespace de `agents/graph.py`).
  - Remover o conjunto literal `HANDOFF_TOOLS` de `agents/graph.py`.
  - Em `build_graph`, derivar `handoff_tools = frozenset(tool.name for tool in all_tools if is_handoff_tool(tool))` a partir das ferramentas efetivamente registradas nos 4 especialistas e vincular esse conjunto ao guard `_guard_tool_call` (via `partial` ou closure), preservando a prioridade de `encerrar_atendimento` e a exclusividade de handoffs em lotes paralelos.
- **Consumo do contrato HITL e do texto higienizado:**
  - Em `resume_turn`: enviar `Command(resume=LimitIncreaseDecision(approved=approved).to_resume_payload())`.
  - Em `_pending_confirmation`: iterar `snapshot.interrupts` e retornar o primeiro `LimitIncreaseConfirmation.from_interrupt_value(item.value)` não nulo.
  - Em `_execute_turn`: ao tratar entrada textual (`isinstance(turn_input, str)`), avaliar `decision = evaluate_user_input(turn_input)` uma única vez e construir `HumanMessage(content=decision.cleaned_text)` diretamente quando permitida (sem repetir `.replace("\x00", "").strip()`).

## Fora de Escopo

- Colocalização do protocolo MCP de câmbio (`F15`).
- Refatoração dos helpers de privacidade/CSV em `evals/runner.py` (Spike — Candidato 6).
- Alterações em `GLOSSARY.md` ou criação/edição de ADRs (os ADRs `003`, `004`, `006` e `009` permanecem integralmente preservados sem novos trade-offs irreversíveis).
- Alterações em `src/banco_agil/ui/app.py`, `src/banco_agil/api/schemas.py`, `src/banco_agil/api/app.py`, `src/banco_agil/runtime.py` ou `src/banco_agil/evals/runner.py`.
- Edição de `docs/backlog.md`, `docs/spec.md` e `AGENTS.md` dentro do worktree (atualizados no fechamento ao integrar na `main`).

## Seams de Teste Pré-Acordados (para execução com `/tdd`)

Seguindo *the interface is the test surface* e *replace, don't layer*:

| Seam | Interface sob teste | O que captura | O que fica fora (coberto por outro seam) |
| :--- | :--- | :--- | :--- |
| **1. Contratos de Agentes** (`tests/test_credit_agent.py` e/ou `tests/test_agent_contracts.py`) | `AGENT_NODES`, `LimitIncreaseConfirmation.to_interrupt_payload()` / `from_interrupt_value()`, `LimitIncreaseDecision.to_resume_payload()` / `is_approved()` | Igualdade `AGENT_NODES == get_args(AgentName)`, round-trip do payload HITL com `"action": "confirm_limit_increase"` sem vazar `action` para `model_dump()`, rejeição de interrupt de outra ação ou não-dict, validação de `NaN`/`inf`, `StrictBool` + `extra="forbid"` em `LimitIncreaseDecision` (rejeitando `1`, `"true"`, `"sim"`, chaves extras e dicts incompletos) | Execução do grafo LangGraph e OpenAPI HTTP |
| **2. Seam de Guardrails** (`tests/test_guardrails.py`) | `evaluate_user_input(message, max_chars=...) -> InputGuardrailDecision` | Retorno de `cleaned_text` sem `\x00` e sem espaços extremos quando permitido, `cleaned_text is None` quando bloqueado (`empty`, `too_long`, `prompt_injection`), invariantes do modelo `InputGuardrailDecision` e uso do texto limpo no `HumanMessage` em `run_turn` | Mascaramento de logs e telemetria Langfuse |
| **3. Ferramentas: Gate de Autenticação e Handoffs** (`tests/test_triage_agent.py`, `tests/test_credit_agent.py`, `tests/test_interview_agent.py`, `tests/test_exchange_agent.py`) | `build_*_tools`, `voltar_para_triagem`, `is_handoff_tool`, `convert_to_openai_tool` | Marcação exata das 5 ferramentas de handoff (`is_handoff_tool`), ausência de vazamento de `extras` no schema enviado ao LLM, gate de Autenticação deduplicado bloqueando acesso sem autenticação em todas as 5 ferramentas sem tocar em serviços | Transporte MCP e serialização HTTP |
| **4. Orquestração do Grafo sem Infra MCP** (`tests/test_walking_skeleton.py`, `tests/test_triage_agent.py`, `tests/test_credit_agent.py`, `tests/test_interview_agent.py`, `tests/test_guardrails.py`, `tests/test_observability.py`, `tests/test_api.py`, `tests/test_mcp_exchange.py`) | `build_graph(model, credit_service, exchange_provider, *, checkpointer=None)`, `run_turn`, `resume_turn` | Exigência explícita de `exchange_provider`, ausência de import `McpExchangeRateProvider` / `mcp_exchange_client` em `agents/graph.py`, deduplicação de handoffs em lotes paralelos derivada das ferramentas registradas, fluxo HITL ponta a ponta preservado | Wiring de produção em `ConversationRuntime` (coberto em `test_runtime.py`) |

### Estratégia *Replace, Don't Layer* nos Testes Existentes

- **`tests/conftest.py`**: adicionar fixture `exchange_provider` fornecendo um stub mínimo que implementa `ExchangeRateProvider` cujo `get_quote` levanta `AssertionError("Provedor de câmbio não esperado neste teste")`, garantindo falha ruidosa se um teste não-câmbio acionar câmbio por acidente sem instanciar servidor MCP.
- **Testes focados sem câmbio** (`tests/test_walking_skeleton.py`, `tests/test_triage_agent.py`, `tests/test_credit_agent.py`, `tests/test_interview_agent.py`, `tests/test_guardrails.py`, `tests/test_observability.py`, `tests/test_api.py`): passar a fixture `exchange_provider` em `build_graph(model, credit_service, exchange_provider)` (mantendo `tests/test_exchange_agent.py` e `tests/test_mcp_exchange.py` injetando seus próprios provedores explícitos).
- **`tests/test_guardrails.py`**: estender os testes de `evaluate_user_input` para verificar `cleaned_text` (permitido vs. bloqueado) e invariantes de `InputGuardrailDecision`, sem duplicar suítes paralelas.
- **`tests/test_credit_agent.py` e `tests/test_triage_agent.py`**: adicionar cobertura direta para os contratos HITL tipados (`LimitIncreaseConfirmation` / `LimitIncreaseDecision`), marcação `is_handoff_tool` + schema OpenAI limpo e verificação de que `agents/graph.py` não importa `mcp_exchange_client` e exige `exchange_provider`.

## Sequência de Fatias Verticais Sugerida (`/tdd`: red → green por fatia)

1. **Fatia 1 — `AGENT_NODES` e contratos HITL tipados (`src/banco_agil/agents/state.py`):**
   - Red → green nos testes de contrato (`AGENT_NODES`, `LimitIncreaseConfirmation`, `LimitIncreaseDecision`) → implementar em `src/banco_agil/agents/state.py`.
2. **Fatia 2 — Seam de Guardrails retornando `cleaned_text` (`src/banco_agil/agents/guardrails.py`):**
   - Red → green em `tests/test_guardrails.py` → adicionar `cleaned_text` e `@model_validator` em `InputGuardrailDecision` e retornar `cleaned_text=cleaned` em `evaluate_user_input`.
3. **Fatia 3 — Gate deduplicado, handoffs marcados e HITL tipado (`src/banco_agil/agents/tools.py`):**
   - Red → green nos testes de ferramentas (`is_handoff_tool`, schema OpenAI limpo, gate de Autenticação e retomada HITL com `extra="forbid"`) → implementar `_require_authenticated_client`, `@tool(extras={"handoff": True})`, `is_handoff_tool` e uso de `LimitIncreaseConfirmation`/`LimitIncreaseDecision` em `src/banco_agil/agents/tools.py`.
4. **Fatia 4 — Orquestração limpa e remoção do import MCP (`src/banco_agil/agents/graph.py` + `tests/`):**
   - Adicionar fixture `exchange_provider` em `tests/conftest.py` e testes de ausência de import MCP / exigência de `exchange_provider` → atualizar `src/banco_agil/agents/graph.py` (`build_graph` exigindo `exchange_provider`, derivação de `handoff_tools` via `is_handoff_tool`, consumo de `decision.cleaned_text`, `LimitIncreaseConfirmation.from_interrupt_value` e `LimitIncreaseDecision.to_resume_payload`) e atualizar os call sites de teste.
5. **Gate final no worktree:** rodar `make check` e `make evals` (100% verdes).

## Critérios de Aceite

- [ ] `AGENT_NODES` é derivado de `AgentName` via `get_args(AgentName)` em `src/banco_agil/agents/state.py` (sem tupla duplicada em `graph.py`).
- [ ] `LimitIncreaseConfirmation` e `LimitIncreaseDecision` concentram as duas direções do protocolo HITL em `src/banco_agil/agents/state.py`, preservando `"action": "confirm_limit_increase"` no payload de `interrupt()`, `StrictBool` + `extra="forbid"` na retomada e os 3 campos públicos de `ChatResponse.pending_confirmation` no OpenAPI.
- [ ] `evaluate_user_input` retorna `cleaned_text` higienizado quando `allowed=True` (e `None` quando bloqueado) com invariante validado em `InputGuardrailDecision`; `agents/graph.py` não repete `.replace("\x00", "").strip()`.
- [ ] `HANDOFF_TOOLS` literal foi removido de `agents/graph.py`; as 5 ferramentas de handoff são marcadas uma única vez com `@tool(extras={"handoff": True})` em `agents/tools.py`, expostas via `is_handoff_tool(tool)` sem vazar `extras` no schema enviado ao LLM, e derivadas em `build_graph`.
- [ ] O gate de Autenticação em `src/banco_agil/agents/tools.py` vive em um único auxiliar (`_require_authenticated_client`), substituindo as 5 cópias e eliminando os 3 `cast(str, runtime.state["client_cpf"])`.
- [ ] `src/banco_agil/agents/graph.py` não importa `McpExchangeRateProvider` nem `banco_agil.infrastructure.mcp_exchange_client`, e `build_graph` exige `exchange_provider: ExchangeRateProvider`.
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`, cobertura geral mantida ≥ 98%).
- [ ] `make evals` 100% verde (12/12 cenários, 0 vazamentos de PII).
- [ ] Commits pequenos em Conventional Commits na branch `feat/f14-agent-contracts-guardrails`, sem editar `docs/backlog.md`, `docs/spec.md`, `GLOSSARY.md`, `AGENTS.md` nem commitar arquivos de tooling local.

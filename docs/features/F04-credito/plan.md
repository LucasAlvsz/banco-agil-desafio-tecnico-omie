# Plano — F04: Agente de Crédito e Human-in-the-Loop (HITL)

## Objetivo
Implementar o **Agente de Crédito** completo integrado ao `CreditService` (consulta de limite disponível, validação determinística de aumento, registro formal em `solicitacoes_aumento_limite.csv`, atualização de `clientes.csv` em aprovações e oferta de entrevista em rejeições) utilizando **`interrupt()` / `Command(resume=...)`** nativo do LangGraph v1 para confirmação explícita do cliente antes de persistir qualquer pedido de aumento.

## Dependências
Nenhuma dependência nova em `pyproject.toml`.

## Escopo

### 1. Estado e Contratos de Confirmação (`src/banco_agil/agents/state.py` e `graph.py`)
- Adicionar em `ConversationState`:
  - `pending_requested_limit: NotRequired[float | None]` *(preserva o valor desejado caso o pedido seja rejeitado e o cliente aceite a entrevista na F05; limpo para `None` em caso de aprovação)*.
- Criar modelo imutável `LimitIncreaseConfirmation(BaseModel)` (sem PII):
  - `limite_atual: float`
  - `novo_limite_solicitado: float`
  - `message: str`
- Estender `TurnResult` com:
  - `pending_confirmation: LimitIncreaseConfirmation | None = None`

### 2. Prompts do Agente de Crédito (`src/banco_agil/agents/prompts.py`)
- Substituir `build_credit_placeholder_prompt` por `build_credit_prompt(state)`:
  - Mantém persona única contínua (sem anunciar transferências).
  - Orienta a usar `consultar_limite` para informar o limite disponível atual (sem inventar valores).
  - Orienta a coletar o novo limite desejado e acionar `solicitar_aumento_limite` (sem pedir confirmação duplicada só por texto quando a ferramenta já gerencia a confirmação estruturada).
  - Quando um pedido for **rejeitado**, instrui a informar claramente o resultado e oferecer o redirecionamento para atualização de score via entrevista (`redirecionar_para_entrevista`); se o cliente recusar, oferecer ajuda em outro assunto (`voltar_para_triagem`) ou encerrar (`encerrar_atendimento`).
  - Se `state.get("pending_requested_limit")` estiver preenchido (preparação para o retorno da entrevista na F05), contextualizar o prompt com esse valor.
- Adicionar `build_interview_placeholder_prompt(state)` (nó mínimo para validar a transição `credito → entrevista` nesta feature; substituído pelo agente completo na F05).

### 3. Ferramentas do Agente de Crédito (`src/banco_agil/agents/tools.py`)
Criar `build_credit_tools(credit_service: CreditService)` retornando ferramentas que leem `client_cpf` **exclusivamente** de `runtime.state` via `ToolRuntime` (nunca de argumentos do LLM):
- `consultar_limite(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
  - Verifica `has_authenticated_client(runtime.state)` (se falso, força retorno à `triagem`).
  - Busca o cliente atualizado em `credit_service.get_client(cpf)` e retorna `ToolMessage` com o `limite_atual` (sem CPF/nascimento).
  - Em `ClientNotFoundError` ou `PersistenceError`: loga apenas `type(exc).__name__` e retorna mensagem controlada sem derrubar a conversa.
- `solicitar_aumento_limite(novo_limite: float, runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
  - **Fase 1 — Pré-`interrupt` (100% idempotente, zero escrita em CSV):**
    - Valida `has_authenticated_client(runtime.state)` (se falso, recusa e mantém/retorna `triagem`).
    - Valida `novo_limite` finito e consulta `client = credit_service.get_client(cpf)`.
    - Se `novo_limite <= client.limite_atual` ou `<= 0` ou não finito: retorna `ToolMessage` estruturada informando o `limite_atual` e que o novo limite precisa ser maior — **sem pausar com `interrupt()` e sem gravar CSV**.
  - **Fase 2 — Pausa HITL (`langgraph.types.interrupt`):**
    - Pausa com payload JSON-serializável sem PII:
      ```python
      decision = interrupt(
          {
              "action": "confirm_limit_increase",
              "limite_atual": client.limite_atual,
              "novo_limite_solicitado": novo_limite,
              "message": ...,
          }
      )
      ```
  - **Fase 3 — Pós-`interrupt` (executa somente após `Command(resume=...)`):**
    - Se `not isinstance(decision, dict) or decision.get("approved") is not True`: retorna `ToolMessage` informando que o pedido foi cancelado pelo cliente (**zero escrita em CSV**).
    - Se aprovado: chama `result = credit_service.process_limit_increase(cpf, novo_limite)`:
      - Se `result.aprovado`: retorna `_tool_result(..., pending_requested_limit=None)` informando aprovação e novo limite vigente.
      - Se rejeitado: retorna `_tool_result(..., pending_requested_limit=novo_limite)` informando que o pedido foi registrado como rejeitado para o score atual e que o cliente pode realizar a entrevista de crédito para reavaliar o score.
    - Tratar `DomainValidationError`, `ClientNotFoundError` e `PersistenceError` de forma controlada, logando apenas `type(exc).__name__`.
- `redirecionar_para_entrevista(runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
  - Gate em código via `has_authenticated_client(runtime.state)`.
  - Se autenticado, atualiza `active_agent = "entrevista"` via `Command(update=...)` (sem `goto`).

### 4. Grafo e Retomada de Interrupção (`src/banco_agil/agents/graph.py`)
- Registrar em `build_graph`:
  - `credito` ↔ `credito_tools = ToolNode([*build_credit_tools(credit_service), voltar_para_triagem, encerrar_atendimento])`
  - `entrevista` ↔ `entrevista_tools = ToolNode([voltar_para_triagem, encerrar_atendimento])` (placeholder mínimo até a F05; incluir `"entrevista"` em `AGENT_NODES` e `"redirecionar_para_entrevista"` em `HANDOFF_TOOLS`).
- Garantir que `_guard_tool_call` e `handle_tool_errors` não capturem `GraphInterrupt` (deixando a pausa propagar normalmente para o runtime do LangGraph).
- Extrair helper `_pending_confirmation(snapshot) -> LimitIncreaseConfirmation | None` inspecionando `snapshot.interrupts`.
- Atualizar `run_turn(...)`:
  - Se já houver confirmação pendente no checkpoint (`snapshot.interrupts`), não envia novo `HumanMessage` ao grafo; retorna `TurnResult` com o mesmo `pending_confirmation` e instrução cordial para confirmar ou cancelar.
  - Após `graph.invoke(...)`, verifica `graph.get_state(config).interrupts`: se pausado, retorna `TurnResult` com `reply=confirmation.message` e `pending_confirmation=confirmation`.
- Criar `resume_turn(graph: ConversationGraph, *, thread_id: str, approved: bool) -> TurnResult`:
  - Se a sessão estiver encerrada → retorna `ENDED_REPLY`.
  - Se não houver interrupção pendente → retorna mensagem controlada sem erro.
  - Invoca `graph.invoke(Command(resume={"approved": approved}), config)` e retorna o `TurnResult` consolidado (com redação de PII preservada e logging seguro em falhas).

### 5. Interface Streamlit com HITL (`src/banco_agil/ui/app.py`)
- Persistir `pending_confirmation` em `st.session_state` (limpando em **Nova conversa**).
- Quando `st.session_state.pending_confirmation` estiver presente:
  - Desabilitar `st.chat_input`.
  - Exibir card/aviso com os valores (`limite_atual` → `novo_limite_solicitado`) e botões **"Confirmar pedido"** e **"Cancelar"**.
  - Ao clicar, acionar `resume_turn(st.session_state.graph, thread_id=..., approved=True/False)`, registrar a resposta no histórico, atualizar `pending_confirmation`/`conversation_ended` e executar `st.rerun()`.

### 6. Documentação Arquitetural (`docs/adr/004-agente-credito-e-hitl.md`)
Seguindo `docs/adr/000-template.md`, registrar:
- Decisão de usar `interrupt()` / `Command(resume=...)` nativo do LangGraph antes de gravar pedidos de aumento (em vez de confirmação apenas por prompt).
- Padrão de idempotência pré-interrupt vs. efeito colateral pós-resume.
- **Mapeamento completo de pontos HITL nos fluxos do Banco Ágil** (fechando o spike em aberto da `docs/spec.md`):
  - Onde HITL faz sentido no MVP (pedido de aumento de limite — efeito colateral financeiro/contratual).
  - Onde **não** usar `interrupt()` no MVP (consulta de limite, consulta de câmbio, autenticação — baixo impacto ou causaria fadiga de confirmação; revisão dos dados da entrevista avaliada como trade-off documentado).
  - Evolução e Escala: HITL assíncrono de **backoffice / analista de risco** (alçada humana para aumentos acima de um teto, sinais de fraude ou divergência cadastral, com fila e retomada por webhook/worker).

## Fora de Escopo
- Coleta das 5 variáveis da entrevista e `calcular_e_atualizar_score` (`F05`).
- Cliente HTTP e tools de Câmbio (`F06`).
- Filtro global de logging LGPD (`F07`), telemetria/Langfuse (`F08`), FastAPI (`F09`) e MCP (`F10`).
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes (100% determinísticos, zero rede)
Criar `tests/test_credit_agent.py` (e estender `tests/test_ui.py`) cobrindo:
1. **Schema seguro (`ToolRuntime`):** `consultar_limite` não recebe parâmetros do LLM; `solicitar_aumento_limite` recebe apenas `novo_limite` (`cpf` nunca no schema).
2. **Consulta de limite:** retorna limite atualizado de `clientes.csv` sem expor CPF/nascimento; trata `PersistenceError` logando só o tipo da exceção.
3. **Validação pré-`interrupt`:** pedido com valor `<= limite_atual`, `0`, negativo ou `NaN` retorna erro estruturado informando o limite atual, **não pausa o grafo** (`pending_confirmation is None`) e **não grava nada no CSV**.
4. **HITL — Cancelamento:** `solicitar_aumento_limite` pausa devolvendo `pending_confirmation` (sem PII) e CSV intacto; `resume_turn(..., approved=False)` retoma sem gravar nada em `solicitacoes_aumento_limite.csv` nem alterar `clientes.csv`.
5. **HITL — Aprovação:** cliente com score suficiente (ex.: Carlos Mendes, limite 5.000, score 720, pede 15.000) → pausa → `resume_turn(..., approved=True)` → grava 1 linha `'aprovado'` em `solicitacoes_aumento_limite.csv`, atualiza `limite_atual = 15000` em `clientes.csv`, limpa `pending_requested_limit = None`; consulta subsequente já retorna R$ 15.000.
6. **HITL — Rejeição + oferta de entrevista:** cliente Ana Souza (limite 2.500, score 420, pede 6.000) → pausa → `resume_turn(..., approved=True)` → grava 1 linha `'rejeitado'` no CSV, mantém `clientes.csv` inalterado, salva `pending_requested_limit = 6000` no estado → cliente aceita → `redirecionar_para_entrevista` transita para `active_agent = "entrevista"` no mesmo turno.
7. **Proteção de estado pausado:** chamar `run_turn` enquanto há confirmação pendente não corrompe o checkpoint e retorna o mesmo `pending_confirmation`; chamar `resume_turn` sem pausa pendente retorna resposta controlada sem erro.
8. **Idempotência de efeitos colaterais:** confirmar que a re-execução da tool no `resume` executa `process_limit_increase` exatamente **1 vez** (1 única linha no CSV).
9. **Isolamento de `ToolNode`s:** `credito_tools` não contém `autenticar_cliente`; `triagem_tools` não contém `consultar_limite` nem `solicitar_aumento_limite`; `entrevista_tools` só contém `voltar_para_triagem` e `encerrar_atendimento`.
10. **UI Streamlit (`AppTest` em `tests/test_ui.py`):** renderização dos botões **Confirmar pedido** / **Cancelar** com `chat_input` desabilitado durante a pausa e retomada correta após clique.

## Critérios de Aceite
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [ ] Cobertura ≥ 90% em `src/banco_agil/agents/`.
- [ ] Zero PII em payloads de `interrupt()`, `TurnResult`, `ToolMessage` ou logs.
- [ ] `docs/adr/004-agente-credito-e-hitl.md` criado e revisado.
- [ ] Commits pequenos em Conventional Commits na branch `feat/f04-credito` (incluindo `plan.md`).

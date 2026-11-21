# Plano — F05: Agente de Entrevista de Crédito

## Objetivo
Substituir o placeholder do nó `entrevista` pelo **Agente de Entrevista de Crédito** completo, conduzindo a coleta conversacional dos 5 indicadores financeiros, recalculando o score de crédito de forma determinística via `CreditService.recalculate_score(...)`, atualizando `clientes.csv` e retornando automaticamente ao Agente de Crédito no mesmo turno para propor a reavaliação do pedido pendente (com confirmação HITL).

## Dependências
Nenhuma dependência nova em `pyproject.toml`.

## Escopo

### 1. Prompts (`src/banco_agil/agents/prompts.py`)
- Substituir `build_interview_placeholder_prompt` por `build_interview_prompt(state)`:
  - Mantém a persona única contínua do Banco Ágil (sem anunciar transferências ou mudanças de agente).
  - Orienta a coletar de forma natural e objetiva, sem repetições desnecessárias, os 5 indicadores financeiros:
    1. Renda mensal (`renda_mensal`, em reais `>= 0`).
    2. Tipo de emprego (`tipo_emprego`: `formal`, `autônomo` ou `desempregado`).
    3. Despesas fixas mensais (`despesas_fixas`, em reais `>= 0`).
    4. Número de dependentes (`num_dependentes`: `0`, `1`, `2` ou `3+`).
    5. Existência de dívidas ativas (`tem_dividas`: `sim` ou `não`).
  - Aceita múltiplos indicadores na mesma resposta do cliente e pergunta apenas o que estiver faltando ou ambíguo.
  - Proíbe calcular o score mentalmente; instrui a acionar `calcular_e_atualizar_score` assim que todos os 5 indicadores forem coletados.
  - Se o cliente desistir da entrevista ou mudar de assunto, orienta a usar `voltar_para_triagem` (ou `encerrar_atendimento` se pedir para encerrar).
- Aprimorar `build_credit_prompt(state)` para o retorno pós-entrevista:
  - Quando o score acabar de ser atualizado e houver `pending_requested_limit` na sessão, orientar o agente a informar o novo score ao cliente e perguntar se deseja reenviar o pedido para esse valor anterior (ou informar um novo valor), acionando `solicitar_aumento_limite` somente quando o cliente confirmar interesse em submeter o pedido.

### 2. Ferramenta da Entrevista (`src/banco_agil/agents/tools.py`)
Criar `build_interview_tools(credit_service: CreditService)` retornando:
- `calcular_e_atualizar_score(renda_mensal: float, tipo_emprego: str, despesas_fixas: float, num_dependentes: int | str, tem_dividas: str | bool, runtime: ToolRuntime[None, ConversationState]) -> Command[Any]`:
  - **Gate em código:** verifica `has_authenticated_client(runtime.state)`. Se falso, retorna `active_agent="triagem"` sem acessar `credit_service`.
  - Lê `cpf` exclusivamente de `runtime.state["client_cpf"]` (`cpf` nunca aparece no schema público da ferramenta).
  - Invoca `credit_service.recalculate_score(cpf, renda_mensal=renda_mensal, despesas_fixas=despesas_fixas, tipo_emprego=tipo_emprego, num_dependentes=num_dependentes, tem_dividas=tem_dividas)`.
  - **Tratamento de erros:**
    - `DomainValidationError`: retorna `ToolMessage` com a orientação de correção de domínio (sem PII) mantendo `active_agent="entrevista"` para o cliente corrigir os dados.
    - `ClientNotFoundError` / `PersistenceError`: registra em `logging` apenas `type(exc).__name__` (sem PII) e retorna `ToolMessage` controlada de indisponibilidade temporária mantendo `active_agent="entrevista"`.
  - **Sucesso:** atualiza `active_agent="credito"` no `Command(update=...)` (sem `goto`) e retorna `ToolMessage` informando o novo score (sem PII) e instruindo a retomar o atendimento de crédito. Como o roteador condicional determinístico pós-`entrevista_tools` lê `active_agent`, o nó `credito` responde ao cliente no **mesmo turno** (handoff implícito).

### 3. Grafo (`src/banco_agil/agents/graph.py`)
- Substituir a configuração placeholder do nó `entrevista` em `build_graph`:
  - `interview_tools = [*build_interview_tools(credit_service), *specialist_tools]`
  - `_add_agent_nodes(graph, "entrevista", model, build_interview_prompt, interview_tools)`
- Incluir `"calcular_e_atualizar_score"` em `HANDOFF_TOOLS` para que `_guard_tool_call` evite conflitos de escrita em `active_agent` caso o modelo emita `calcular_e_atualizar_score` em paralelo com `voltar_para_triagem` no mesmo lote (mantendo prioridade absoluta para `encerrar_atendimento`).

## Fora de Escopo
- Cliente HTTP e ferramentas do Agente de Câmbio (`F06`).
- Filtro global de mascaramento LGPD em logs (`F07`), telemetria/Langfuse (`F08`), FastAPI (`F09`) e MCP (`F10`).
- Nenhum ADR novo é necessário nesta feature (fórmula de score, handoffs e HITL já estão documentados nos ADRs 001, 003 e 004).
- Não editar `docs/backlog.md`, `docs/spec.md` nem `AGENTS.md` dentro do worktree.

## Estratégia de Testes (100% determinísticos, zero rede)
Criar `tests/test_interview_agent.py` (e ajustar eventuais asserts de placeholder em `tests/test_credit_agent.py` se afetados) cobrindo:
1. **Schema seguro e gate pré-auth (`ToolRuntime`):** `calcular_e_atualizar_score` expõe exatamente os 5 parâmetros financeiros (`cpf` nunca no schema); invocação sem sessão autenticada retorna `active_agent="triagem"` sem chamar `credit_service`.
2. **Recálculo bem-sucedido e handoff implícito no mesmo turno:** atualiza `score` em `clientes.csv` (preservando `limite_atual` e demais clientes), retorna `ToolMessage` sem PII e entrega o controle diretamente para `credito` no mesmo turno (`active_agent == "credito"`).
3. **Erros de validação de domínio (`DomainValidationError`):** valores inválidos (`renda_mensal < 0`, `despesas_fixas = NaN`, `tipo_emprego` desconhecido, `num_dependentes` inválido, `tem_dividas` inválido) mantêm `active_agent="entrevista"` e deixam `clientes.csv` intacto.
4. **Erros de persistência / cliente ausente (`PersistenceError`, `ClientNotFoundError`):** logam apenas `type(exc).__name__` (sem PII e sem `exc_info`), mantêm `active_agent="entrevista"` e deixam `clientes.csv` intacto.
5. **Jornada completa ponta a ponta (Ana Souza — rejeição → entrevista → aprovação):**
   - Triagem autentica `123.456.789-01` / `14/05/1990` e redireciona para Crédito.
   - Crédito solicita R$ 6.000 → `interrupt()` → `resume_turn(approved=True)` → pedido `rejeitado` gravado no CSV, `clientes.csv` intacto, `pending_requested_limit == 6000`.
   - Cliente aceita entrevista → handoff para `entrevista` → informa os 5 dados (`renda=6000, formal, despesas=2000, dependentes=0, dividas=não`) → score atualizado para `590` em `clientes.csv` → handoff automático para `credito` no mesmo turno com `pending_requested_limit == 6000` presente no prompt.
   - Cliente confirma reenvio de R$ 6.000 → `solicitar_aumento_limite(6000)` → `interrupt()` → `resume_turn(approved=True)` → pedido `aprovado` gravado como **2ª linha** em `solicitacoes_aumento_limite.csv`, `limite_atual == 6000` e `score == 590` em `clientes.csv`, `pending_requested_limit is None`.
6. **Desistência / encerramento durante a entrevista:** `voltar_para_triagem` ou `encerrar_atendimento` no meio da entrevista preservam `clientes.csv` intacto.
7. **Lote paralelo (`calcular_e_atualizar_score` + `voltar_para_triagem` ou duplicada):** apenas 1 transição de `active_agent` executa por lote sem erro de `tool_call_id` órfão; `encerrar_atendimento` mantém prioridade.
8. **Isolamento físico de `entrevista_tools`:** contém apenas `calcular_e_atualizar_score`, `voltar_para_triagem` e `encerrar_atendimento`.

## Critérios de Aceite
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [ ] Cobertura ≥ 95% em `src/banco_agil/agents/`.
- [ ] Zero PII em `ToolMessage`, `TurnResult` ou logs.
- [ ] Commits pequenos em Conventional Commits na branch `feat/f05-entrevista` (incluindo `plan.md`).

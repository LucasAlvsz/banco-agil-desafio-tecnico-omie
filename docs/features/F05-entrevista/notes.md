# Notas — F05: Agente de Entrevista de Crédito

## Entregue
- Prompt estruturado do Agente de Entrevista de Crédito (`build_interview_prompt`) coletando os 5 indicadores financeiros e ajuste em `build_credit_prompt` para o retorno pós-entrevista com proposta de reenvio do `pending_requested_limit`.
- Ferramenta `calcular_e_atualizar_score` em `entrevista_tools` isolado, utilizando `ToolRuntime` (sem `cpf` no schema) e tipos estritos (`StrictFloat`, `StrictInt`, `StrictBool`) contra coerção silenciosa.
- Atualização de `clientes.csv` via `CreditService.recalculate_score(...)` e handoff implícito automático para `credito` no mesmo turno; inclusão em `HANDOFF_TOOLS` para consistência em lotes paralelos.
- Suíte `tests/test_interview_agent.py` cobrindo a jornada completa ponta a ponta (rejeição → entrevista → novo score → retorno ao crédito → confirmação HITL → 2º pedido aprovado), desistência, erros de domínio/persistência e lotes paralelos.

## Revisão
- Cross-model review aprovado de primeira (incluindo verificação do JSON Schema gerado), sem ajustes de código necessários.

## Verificação
- `make check`: `ruff`, `mypy --strict` (19 arquivos) e 209 testes passando (99% de cobertura total; 100% em `graph.py`, `prompts.py`, `state.py`; 99% em `tools.py`).

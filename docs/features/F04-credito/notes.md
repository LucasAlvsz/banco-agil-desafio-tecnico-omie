# Notas — F04: Agente de Crédito e Human-in-the-Loop (HITL)

## Entregue
- Ferramentas autenticadas do Agente de Crédito via `ToolRuntime`: `consultar_limite`, `solicitar_aumento_limite` e `redirecionar_para_entrevista` em `credito_tools` isolado (sem expor `cpf` no schema do modelo).
- Confirmação estruturada antes de persistir pedidos usando `interrupt()` e `Command(resume=...)` nativos do LangGraph v1, com fase pré-`interrupt` idempotente, `LimitIncreaseConfirmation` sem PII e `resume_turn(...)`.
- Proteção de checkpoint contra mensagens de texto durante confirmação pendente e suporte a revalidação de limite na retomada.
- UI Streamlit atualizada com exibição formatada dos valores (`limite_atual → novo_limite_solicitado`), botões **Confirmar pedido** / **Cancelar** e bloqueio do input durante a pausa.
- Nó mínimo `entrevista` registrado para validar o handoff `credito → entrevista` após rejeição.
- Decisões e mapeamento de pontos HITL registrados em `docs/adr/004-agente-credito-e-hitl.md`.

## Revisão
- Cross-model review aprovado de primeira, sem ajustes de código necessários.

## Verificação
- `make check`: `ruff`, `mypy --strict` (19 arquivos) e 188 testes passando (99% de cobertura total; 100% em `graph.py`, `prompts.py`, `state.py`; 99% em `tools.py`; 98% em `ui/app.py`).

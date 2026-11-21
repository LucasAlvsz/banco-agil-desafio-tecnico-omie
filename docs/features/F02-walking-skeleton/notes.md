# F02 — Walking skeleton

Implementado o escopo de `plan.md`: configuração tipada, factory LiteLLM,
grafo LangGraph com histórico por sessão e encerramento global, e chat Streamlit.
O ADR 002 registra as decisões e os caminhos de evolução.

## Validação

- `make check`: Ruff, formatação e mypy strict aprovados; 137 testes passaram.
- Cobertura: 100% em `config.py`, `llm_factory.py` e `agents/`; 92% no pacote.
  A UI foi exercitada por quatro testes `AppTest`, embora a execução do script pelo
  Streamlit não tenha sido contabilizada no relatório de cobertura de linhas.
- `make ui`: servidor iniciado e `/_stcore/health` respondeu HTTP 200 (`ok`).
- Smoke com Groq real **aprovado** em 2026-11-21, fora do pytest, com
  `groq/openai/gpt-oss-120b` e credenciais carregadas de um `.env` local externo ao
  worktree, sem copiar ou registrar segredos. A execução pelo grafo confirmou uma
  saudação, `encerrar_atendimento` via `ToolNode` (com `ToolMessage` no checkpoint),
  despedida sem tools e bloqueio de novos turnos na sessão encerrada. Um callback
  contou três invocações do modelo, com tools vinculadas em `[True, True, False]`;
  um turno adicional após o encerramento não aumentou essa contagem.

## Revisão

Aplicados os três ajustes do handoff de review:

- `conversation_ended` é opcional no estado de entrada; `run_turn` envia somente
  mensagens e preserva a flag persistida. O teste de invocação direta após o
  encerramento omite a flag e confirma que o modelo não é chamado novamente.
- Falhas capturadas por `run_turn` registram somente a classe da exceção em log.
  O teste com `caplog` confirma ausência da mensagem bruta, traceback e stack info.
- Evidências pontuais de validação foram movidas do ADR 002 para estas notas.

O smoke real acima foi executado antes dos ajustes de review; após as correções,
a validação foi feita pela suíte determinística, sem repetir chamadas ao provedor.

## Limites

Histórico e checkpoints permanecem em memória, conforme o plano e o ADR 002.
Sem integração na main nesta entrega. `AGENTS.md` e `docs/backlog.md` preservados.

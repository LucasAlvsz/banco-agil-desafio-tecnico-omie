# F03 — Triagem e autenticação

A triagem autentica contra `clientes.csv` e encerra a sessão na terceira falha
consecutiva com dados bem-formados. Crédito e Câmbio usam nós e ferramentas
isolados pelo grafo do ADR 003. Os dois canais informam a indisponibilidade das
operações e permitem voltar à triagem com a sessão autenticada.

## Revisão

- Triagem, ferramentas e roteador exigem a flag de autenticação e um CPF na
  sessão. Se a identidade estiver incompleta, a triagem solicita autenticação.
- As respostas omitem CPF em qualquer etapa. Após autenticar, o mascaramento
  compara as datas da resposta com a data de nascimento confirmada no checkpoint
  e preserva datas operacionais diferentes. Sessões sem uma data confirmada
  continuam mascarando datas numéricas de forma conservadora.

## Validação

- `make check` passou com Ruff, formatação, mypy estrito e 159 testes.
- A suíte usa CSVs temporários, LLM simulado e bloqueio de rede no pytest.
- A cobertura de linhas foi de 190 de 191 em `agents/` e 520 de 522 no pacote.

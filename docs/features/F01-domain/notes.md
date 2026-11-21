# F01 — Domínio e persistência CSV

Implementado o escopo de `plan.md`: contratos Pydantic, normalização, score,
avaliação em degraus, Protocols, serviço de crédito, repositórios CSV e seeds.

## Validação

TDD com testes escritos e executados antes das implementações de domínio e
persistência/serviço. Os ciclos iniciais falharam na coleta por dependência ou
módulos ausentes; após implementação, os cenários passaram.

`make check`: Ruff, formatação e mypy strict verdes; 82 testes passando.
Cobertura de linhas: 100% nos módulos de domínio e infraestrutura (248 linhas
executáveis no pacote completo). Testes de CSV usam diretórios temporários;
a verificação dos seeds reais é somente leitura.

## Limites

ADR 001 registra ausência de transação entre arquivos e de controle de concorrência.
Sem integração na main nesta entrega. `AGENTS.md` e `docs/backlog.md` preservados.

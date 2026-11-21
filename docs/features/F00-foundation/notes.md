# Notas — F00: Fundação do Projeto

## Entregue
- Ambiente Python 3.12 via `uv` com pacote `src/banco_agil/`, `py.typed` e smoke test em `tests/test_smoke.py`.
- Gate de qualidade local configurado (`ruff`, `mypy --strict` em `src/`, `pytest` + `pytest-cov`, `pre-commit` e `Makefile`).
- Documentação base estabelecida: `AGENTS.md`, `docs/spec.md`, `docs/backlog.md`, `docs/adr/000-template.md` e `README.md` inicial.

## Ajustes em Relação ao Plano Original
- **Isolamento por `git worktree`:** definido que a partir da `F01` cada feature será desenvolvida em worktree isolado na pasta irmã `../banco-agil-worktrees/`, evitando conflitos entre branches paralelas e contaminação de contexto entre agentes.
- **Fronteira clara entre `AGENTS.md` e `docs/spec.md`:** `AGENTS.md` foi mantido estritamente operacional/engenharia (sem enumerar regras de negócio voláteis), deixando todas as regras de negócio e classificação de PII centralizadas em `docs/spec.md`.
- **Framing de evolução e escala:** seção final do template de ADR e da spec estruturada como *Evolução e Escala*, registrando limites intencionais do MVP e seus gatilhos de evolução.

## Verificação
- `make setup` e `make check` executados com sucesso (100% verde, todos os hooks do `pre-commit` validados).

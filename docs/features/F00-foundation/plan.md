# Plano — F00: Fundação do Projeto

## Objetivo
Montar a base de engenharia, qualidade, controle de versão e documentação viva antes da primeira linha de regra de negócio, sem antecipar dependências de agentes ou UI.

## Escopo
- Ambiente Python 3.12 gerenciado por `uv` (`pyproject.toml`, `.python-version`, `.gitignore`, `.env.example`).
- Gate de qualidade local: `ruff`, `mypy --strict` em `src/`, `pytest` + `pytest-cov`, `pre-commit` e `Makefile`.
- Esqueleto mínimo do pacote `src/banco_agil/` (`__init__.py`, `py.typed`) e `tests/test_smoke.py`.
- Documentação viva: `AGENTS.md` (com ciclo de vida baseado em `git worktree` a partir da F01), `docs/spec.md`, `docs/backlog.md`, `docs/adr/000-template.md`, `README.md` inicial e este plano.

## Fora de Escopo
- Código de domínio e seeds CSV (`F01`).
- LangGraph, LiteLLM, Streamlit (`F02`).
- Arquivo `.env` real com chaves de API.
- ADRs técnicos (criados junto às features que concretizam cada decisão).

## Critérios de Aceite
- [ ] `make setup` cria `.venv` (Python 3.12) e instala hooks do `pre-commit`.
- [ ] `make check` passa 100% (`ruff check`, `ruff format --check`, `mypy src`, `pytest --cov=banco_agil`).
- [ ] Histórico git com commit inicial na `main` + branch `feat/f00-foundation` com commits em Conventional Commits.
- [ ] Documentos revisados e limpos antes do `merge --no-ff`.

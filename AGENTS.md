# AGENTS.md — Banco Ágil

Guia operacional, convenções e fluxo de trabalho deste repositório. Este arquivo cresce conforme novas features são entregues — manter sempre fiel ao estado real do código.

## Estrutura Atual

```text
banco-agil/
├── data/               # Seeds CSV (clientes, score_limite, solicitacoes_aumento_limite)
├── src/banco_agil/     # Código da aplicação (Python 3.12, mypy --strict)
│   ├── config.py       # Settings tipadas (pydantic-settings, SecretStr)
│   ├── domain/         # Enums, erros, modelos Pydantic, regras puras, Protocols e CreditService
│   ├── infrastructure/ # Repositórios CSV (escrita atômica/append-only) e factory LiteLLM
│   ├── agents/         # Estado tipado, ferramentas LangGraph e grafo conversacional
│   └── ui/             # Interface conversacional Streamlit
├── tests/              # Testes determinísticos (zero chamadas reais de LLM/rede)
├── docs/
│   ├── spec.md         # Especificação do produto (fonte única da verdade)
│   ├── backlog.md      # Quadro Kanban (To Do / Doing / Done)
│   ├── adr/            # Architecture Decision Records (000-template.md, 001-..., 002-...)
│   └── features/       # plan.md (antes) e notes.md (depois) por feature
├── Makefile            # Comandos padronizados de setup, UI e verificação
└── pyproject.toml      # Configuração do pacote, ruff, mypy e pytest
```

## Comandos

- `make setup` — instala dependências via `uv sync` e configura hooks do `pre-commit`.
- `make format` — corrige lint automaticamente e formata com `ruff`.
- `make test` — executa `pytest` com relatório de cobertura em `src/banco_agil`.
- `make check` — **gate obrigatório** antes de qualquer merge (`ruff check`, `ruff format --check`, `mypy src`, `pytest`).
- `make ui` — sobe a interface conversacional Streamlit (`src/banco_agil/ui/app.py`).

## Princípios e Convenções

1. **Separação entre lógica determinística e LLM:** regras de negócio, validações críticas e persistência residem em código determinístico; o LLM atua na camada conversacional e no acionamento de ferramentas. Consulte `docs/spec.md` para as regras vigentes.
2. **Idioma:** código e commits em inglês; contratos de domínio (schemas de dados, enums de negócio, prompts e UI) e documentação (`README`, `spec`, ADRs) em português.
3. **Sem antecipação (YAGNI):** adicionar dependências e módulos apenas na feature que efetivamente os utiliza.
4. **Evolução e escala:** toda decisão de escopo do MVP com trade-offs conhecidos (ex.: persistência local, processamento síncrono) deve registrar em ADR seus limites atuais e caminhos de evolução para escala.
5. **Segredos e privacidade:** nunca commitar `.env` ou credenciais; nunca logar dados pessoais (PII) em texto claro (veja os campos sensíveis em `docs/spec.md`).

## Ciclo de Vida de uma Feature (`git worktree`)

A partir da `F01`, cada feature é desenvolvida em um worktree isolado na pasta irmã `../banco-agil-worktrees/`:

1. Criar o worktree e a branch a partir da `main`:
   ```bash
   git worktree add ../banco-agil-worktrees/fNN-nome -b feat/fNN-nome main
   ```
   Cada agente ou desenvolvedor atua exclusivamente dentro do worktree da sua feature.
2. Escrever `docs/features/FNN-nome/plan.md` (escopo, fora de escopo, testes, critérios de aceite) e aprovar o plano antes de codar.
3. Implementar em commits pequenos usando **Conventional Commits** (`feat:`, `fix:`, `test:`, `docs:`, `build:`, `refactor:`).
4. Se a feature concretizar uma decisão arquitetural relevante, criar o ADR correspondente em `docs/adr/` a partir de `000-template.md`.
5. Rodar `make check` (100% verde) e revisar o diff.
6. No fechamento: escrever `docs/features/FNN-nome/notes.md`, integrar na `main` com `git merge --no-ff`, atualizar `docs/backlog.md` (e este `AGENTS.md` se estrutura/comandos mudarem) e remover o worktree (`git worktree remove ../banco-agil-worktrees/fNN-nome`).

> **Isolamento:** evitar editar `docs/backlog.md` e `AGENTS.md` durante o desenvolvimento dentro da branch; atualizá-los no fechamento/integração reduz conflitos entre worktrees.

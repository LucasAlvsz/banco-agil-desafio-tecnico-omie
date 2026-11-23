# Plano — F16: Alinhamento do Executor de Evals às Costuras de Privacidade e CSV

## Objetivo

Alinhar o executor de evals (`src/banco_agil/evals/runner.py`) às costuras existentes de **Privacidade** (`src/banco_agil/infrastructure/privacy.py`) e **Adaptadores CSV** (`src/banco_agil/infrastructure/csv_repositories.py`), conforme o **Candidato 6** da revisão arquitetural (`architecture-review-20261121-080832.html`), preservando integralmente os ADRs [001](../../adr/001-dominio-e-persistencia-csv.md), [006](../../adr/006-guardrails-robustez-e-lgpd.md) e [007](../../adr/007-evals-e-observabilidade.md).

A mudança resolve três vazamentos concretos da arquitetura atual:

1. **Importação de regexes internas de privacidade:** `src/banco_agil/evals/runner.py` é o único módulo externo que importa `CPF_PATTERN` e `DATE_PATTERN` de `src/banco_agil/infrastructure/privacy.py` para reimplementar a detecção de **Dados Pessoais Sensíveis (PII)** em `_seed_pii`.
2. **Leitura manual de CSV contornando os adaptadores:** `src/banco_agil/evals/runner.py` usa `csv.DictReader` diretamente para ler `clientes.csv` (mesmo já instanciando `CsvClientRepository` na linha anterior) e `solicitacoes_aumento_limite.csv` (contornando a validação rigorosa de cabeçalho/colunas de `_read()` e a verificação de CPFs duplicados).
3. **Testes acoplados a helper privado:** `tests/test_evals.py` chama diretamente `runner._seed_pii(...)` em vez de exercitar a interface pública do módulo de Privacidade.

## Escopo

### 1. Seam de Detecção de PII e Encapsulamento das Regexes (`src/banco_agil/infrastructure/privacy.py`)

- Tornar privadas ao módulo as expressões regulares compiladas:
  - `_CPF_PATTERN` (antes `CPF_PATTERN`)
  - `_DATE_PATTERN` (antes `DATE_PATTERN`)
  - `_SECRET_PATTERN` (antes `SECRET_PATTERN`)
  - Manter `REDACTED_PLACEHOLDER` público e atualizar `mask_pii` para usar as constantes privadas.
- Adicionar a função pública `contains_pii(text: str, *, cpfs: Collection[str] = (), birth_dates: Collection[str] = ()) -> bool`:
  - Estritamente alinhada à definição de **Dados Pessoais Sensíveis (PII)** de `GLOSSARY.md` (somente CPF e data de nascimento; credenciais continuam responsabilidade de `mask_pii` / `_SECRET_PATTERN`, sem misturar conceitos neste predicado).
  - Normalizar previamente as coleções fornecidas pelo chamador com `normalize_cpf` e `normalize_birth_date` (aceitando valores formatados ou canônicos e falhando rápido com `DomainValidationError` caso o chamador informe um CPF ou data de nascimento inválidos).
  - Quando `cpfs` não for vazio: percorrer `_CPF_PATTERN.finditer(text)` e retornar `True` se `normalize_cpf(match.group())` pertencer ao conjunto normalizado.
  - Quando `birth_dates` não for vazio: percorrer `_DATE_PATTERN.finditer(text)` e retornar `True` se `normalize_birth_date(match.group())` pertencer ao conjunto normalizado, ignorando datas de calendário inválidas encontradas no texto livre (`except DomainValidationError: continue`, ex.: `"32/05/1990"`) e preservando datas operacionais legítimas (ex.: data de referência do BCE `"2026-11-20"` na **Cotação de Câmbio**).
  - Retornar `False` quando não houver correspondência ou quando ambas as coleções estiverem vazias.

### 2. Leitura Tipada nos Adaptadores CSV Concretos (`src/banco_agil/infrastructure/csv_repositories.py`)

- **`CsvClientRepository`:**
  - Promover `_clients(self) -> list[Client]` para o método público `list_all(self) -> list[Client]`, preservando `_read(self.path, Client)` e a validação de CPFs duplicados (`PersistenceError("Cadastro CSV duplicado")`).
  - Atualizar `get()` e `update()` para delegar a `self.list_all()`.
- **`CsvLimitRequestRepository`:**
  - Adicionar o método público `list_all(self) -> list[LimitIncreaseRequest]`:
    - Se `not self.path.exists() or self.path.stat().st_size == 0`, retornar `[]` (simétrico ao contrato de `append()` para um log append-only antes da primeira **Solicitação de Aumento de Limite**), convertendo eventual `OSError` em `PersistenceError("Não foi possível ler os dados CSV") from None`.
    - Quando houver conteúdo no arquivo, delegar estritamente a `_read(self.path, LimitIncreaseRequest)` (levantando `PersistenceError` em cabeçalho ou linha corrompidos).
- **Preservação dos `Protocol`s de domínio (`src/banco_agil/domain/repositories.py`):**
  - Manter `ClientRepository` e `LimitRequestRepository` inalterados (`list_all()` existe apenas nos adaptadores CSV concretos e em `ScoreLimitRepository`), evitando impor varredura completa aos contratos de domínio do `CreditService` e respeitando o caminho de escala do [ADR 001](../../adr/001-dominio-e-persistencia-csv.md).

### 3. Alinhamento do Executor de Evals (`src/banco_agil/evals/runner.py`)

- Remover `import csv`, a função privada `_seed_pii`, a importação de regexes de privacidade (`CPF_PATTERN`, `DATE_PATTERN`) e os imports de domínio usados exclusivamente por esses helpers (`DomainValidationError`, `Client`, `LimitIncreaseRequest`, `normalize_birth_date`, `normalize_cpf`).
- Importar `contains_pii` de `banco_agil.infrastructure.privacy` e `CsvClientRepository`, `CsvLimitRequestRepository` de `banco_agil.infrastructure.csv_repositories`.
- Em `_run_scenario`:
  - Instanciar `clients = CsvClientRepository(clients_path)` e `requests = CsvLimitRequestRepository(requests_path)`.
  - Carregar os **Clientes** iniciais via `seeds = clients.list_all()`, mantendo `cpfs = {client.cpf for client in seeds}` e `dates = {client.data_nascimento for client in seeds}`.
  - Substituir `_seed_pii(result.reply, cpfs, dates)` por `contains_pii(result.reply, cpfs=cpfs, birth_dates=dates)`.
  - Atualizar `_requests(requests: CsvLimitRequestRepository) -> tuple[EvalLimitRequest, ...]` para ler os registros via `requests.list_all()` e projetá-los para `EvalLimitRequest` (excluindo `data_hora_solicitacao`).

## Fora de Escopo

- Unificação entre `EvalScriptedChatModel` (`src/banco_agil/evals/runner.py`) e `ScriptedChatModel` (`tests/conftest.py`) — mantidos separados conscientemente por YAGNI e para não vazar lógica de spy (`calls`, `bindings`) e injeção de exceções de testes para dentro do pacote `src/banco_agil/`.
- Alteração nos `Protocol`s de domínio (`ClientRepository`, `LimitRequestRepository`, `ScoreLimitRepository`, `ExchangeRateProvider`) ou no `CreditService`.
- Adição de parâmetros/fábricas artificiais em `run_evals` apenas para remover o `monkeypatch` de `ConversationRuntime.run_turn` em `tests/test_evals.py` (esse teste já cruza a costura de `ConversationRuntime`).
- Alterações em `GLOSSARY.md` ou criação/edição de ADRs (os ADRs `001`, `006` e `007` permanecem integralmente preservados).
- Edição de `docs/backlog.md`, `docs/spec.md`, `README.md` e `AGENTS.md` dentro do worktree (atualizados no fechamento ao integrar na `main`).

## Seams de Teste Pré-Acordados (para execução com `/tdd`)

Seguindo *the interface is the test surface* e *replace, don't layer*:

| Seam | Interface sob teste | O que captura | O que fica fora (coberto por outro seam) |
| :--- | :--- | :--- | :--- |
| **1. Privacidade (`contains_pii` e encapsulamento)** (`tests/test_privacy.py`) | `contains_pii(text, *, cpfs=..., birth_dates=...)` e superfície do módulo `banco_agil.infrastructure.privacy` | Detecção de CPF com e sem pontuação, datas `YYYY-MM-DD`/`DD/MM/YYYY`/`DD-MM-YYYY`/`DD.MM.YYYY`, normalização de entradas `cpfs`/`birth_dates`, preservação de datas operacionais (ex.: `2026-11-20`), tolerância a datas inválidas no texto livre (`32/05/1990`), não-disparo em CPFs desconhecidos ou coleções vazias, falha rápida (`DomainValidationError`) para `cpfs`/`birth_dates` inválidos e confirmação de que `CPF_PATTERN`/`DATE_PATTERN`/`SECRET_PATTERN` não são mais exportados publicamente | Execução do grafo conversacional e relatório de evals |
| **2. Leitura nos adaptadores CSV** (`tests/test_csv_repositories.py`) | `CsvClientRepository.list_all()` e `CsvLimitRequestRepository.list_all()` | Leitura tipada de todos os **Clientes** (incluindo rejeição de CPF duplicado e CSV corrompido), leitura de **Solicitações de Aumento de Limite** retornando `[]` para arquivo ausente, vazio (`0` bytes) ou apenas com cabeçalho, retorno de múltiplos registros após `append()`, e `PersistenceError` para cabeçalho/linha inválidos ou falha de `OSError` | Agregação de `EvalLimitRequest` e execução de cenários |
| **3. Executor de Evals** (`tests/test_evals.py`) | `run_evals(...)` e CLI `main(...)` | 12/12 cenários offline preservando seeds (incluindo cenário de câmbio fallback com data operacional `2026-11-20` sem falso positivo de PII), detecção ponta a ponta de vazamento de PII e erro de rota retornando exit code `1`, modo `live` com handlers independentes e `flush()` único, e ausência de `runner._seed_pii` / `csv.DictReader` / regexes de privacidade no runner | Casos de borda granulares de regex/datas (cobertos no Seam 1) e parsing CSV corrompido (coberto no Seam 2) |

### Estratégia *Replace, Don't Layer* nos Testes Existentes

- **`tests/test_privacy.py`**: adicionar cobertura direta para `contains_pii` (substituindo as asserções que antes viviam em `tests/test_evals.py` contra `runner._seed_pii`) e verificar que `CPF_PATTERN`, `DATE_PATTERN` e `SECRET_PATTERN` não existem mais como atributos públicos do módulo.
- **`tests/test_csv_repositories.py`**: estender a suíte existente com testes para `CsvClientRepository.list_all()` e `CsvLimitRequestRepository.list_all()` (ausente, `0` bytes, com cabeçalho vazio, após `append()`, corrompido e `OSError`).
- **`tests/test_evals.py`**: remover as 3 linhas que chamavam `runner._seed_pii(...)` diretamente em `test_evals_detect_route_mismatch_and_pii_then_exit_one`, mantendo a verificação ponta a ponta via `main(["--dataset", ..., "--json"])` (e opcionalmente verificando ausência de `_seed_pii` em `runner`).

## Sequência de Fatias Verticais Sugerida (`/tdd`: red → green por fatia)

1. **Fatia 1 — `contains_pii` e encapsulamento das regexes em `src/banco_agil/infrastructure/privacy.py`:**
   - Red → green em `tests/test_privacy.py` para `contains_pii` e privacidade de `_CPF_PATTERN`/`_DATE_PATTERN`/`_SECRET_PATTERN` → implementar em `src/banco_agil/infrastructure/privacy.py`.
2. **Fatia 2 — `list_all()` em `CsvClientRepository` e `CsvLimitRequestRepository`:**
   - Red → green em `tests/test_csv_repositories.py` para `CsvClientRepository.list_all()` e `CsvLimitRequestRepository.list_all()` (incluindo arquivo ausente, vazio, populado, corrompido e `OSError`) → implementar em `src/banco_agil/infrastructure/csv_repositories.py`.
3. **Fatia 3 — Alinhamento de `src/banco_agil/evals/runner.py` e limpeza de `tests/test_evals.py`:**
   - Remover chamadas a `runner._seed_pii` de `tests/test_evals.py` → migrar `src/banco_agil/evals/runner.py` para `contains_pii`, `CsvClientRepository.list_all()` e `CsvLimitRequestRepository.list_all()`, removendo `import csv`, `_seed_pii` e imports redundantes.
4. **Gate final no worktree:** rodar `make check` e `make evals` (100% verdes).

## Critérios de Aceite

- [ ] `src/banco_agil/infrastructure/privacy.py` expõe `contains_pii(text, *, cpfs=(), birth_dates=()) -> bool` (estritamente CPF e data de nascimento conforme `GLOSSARY.md`) e encapsula `_CPF_PATTERN`, `_DATE_PATTERN` e `_SECRET_PATTERN` como privados.
- [ ] `CsvClientRepository.list_all()` e `CsvLimitRequestRepository.list_all()` existem em `src/banco_agil/infrastructure/csv_repositories.py`, com `CsvLimitRequestRepository.list_all()` retornando `[]` para arquivo ausente/vazio e validando estritamente via `_read()` quando houver conteúdo.
- [ ] `src/banco_agil/domain/repositories.py` (`ClientRepository`, `LimitRequestRepository`) permanece inalterado.
- [ ] `src/banco_agil/evals/runner.py` não importa `csv`, não importa regexes de `privacy.py` e não define `_seed_pii`, atravessando apenas `contains_pii`, `CsvClientRepository` e `CsvLimitRequestRepository`.
- [ ] `tests/test_evals.py` não chama funções privadas do runner (`_seed_pii`); casos unitários de detecção de PII vivem em `tests/test_privacy.py` e leitura CSV em `tests/test_csv_repositories.py`.
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`, cobertura geral mantida ≥ 98%).
- [ ] `make evals` 100% verde (12/12 cenários, 0 vazamentos de PII).
- [ ] Commits pequenos em Conventional Commits na branch `feat/f16-evals-privacy-csv-seams`, sem editar `docs/backlog.md`, `docs/spec.md`, `GLOSSARY.md`, `README.md`, `AGENTS.md` nem commitar arquivos de tooling local.

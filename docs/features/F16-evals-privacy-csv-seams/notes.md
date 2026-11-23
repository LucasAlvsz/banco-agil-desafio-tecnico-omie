# Notas — F16: Alinhamento do Executor de Evals às Costuras de Privacidade e CSV

## Entregue
- **Seam de detecção de PII e encapsulamento das regexes (`src/banco_agil/infrastructure/privacy.py`):**
  - Encapsuladas como privadas ao módulo as expressões regulares `_CPF_PATTERN`, `_DATE_PATTERN` e `_SECRET_PATTERN`, mantendo `REDACTED_PLACEHOLDER` público e preservando o comportamento de `mask_pii`.
  - Exposta a função pública `contains_pii(text: str, *, cpfs: Collection[str] = (), birth_dates: Collection[str] = ()) -> bool`, estritamente alinhada a **Dados Pessoais Sensíveis (PII)** em `GLOSSARY.md` (CPF e data de nascimento, sem misturar credenciais), validando e normalizando previamente os valores informados (`DomainValidationError`), preservando datas operacionais legítimas (como a data de referência do BCE `2026-11-20` na **Cotação de Câmbio**) e ignorando datas de calendário inválidas no texto livre.
- **Leitura tipada nos adaptadores CSV concretos (`src/banco_agil/infrastructure/csv_repositories.py`):**
  - Promovido `CsvClientRepository._clients` para `list_all(self) -> list[Client]`, preservando `_read(self.path, Client)` e a rejeição de CPFs duplicados, com `get()` e `update()` delegando a `self.list_all()`.
  - Adicionado `CsvLimitRequestRepository.list_all(self) -> list[LimitIncreaseRequest]` retornando `[]` para arquivo ausente ou de `0` bytes, delegando arquivos com conteúdo a `_read(self.path, LimitIncreaseRequest)` e convertendo `OSError` (incluindo falha de permissão ou symlink circular) em `PersistenceError("Não foi possível ler os dados CSV") from None`.
  - Extraído o helper privado `_is_empty(self) -> bool` compartilhado entre `list_all()` e `append()`.
  - Preservados inalterados os `Protocol`s de domínio em `src/banco_agil/domain/repositories.py` e o `CreditService`, conforme o ADR 001.
- **Alinhamento do executor de evals (`src/banco_agil/evals/runner.py`):**
  - Removidos `import csv`, `_seed_pii`, a importação de regexes de `privacy.py` e os imports de domínio redundantes (`DomainValidationError`, `Client`, `LimitIncreaseRequest`, `normalize_birth_date`, `normalize_cpf`).
  - Migrado `_run_scenario` e `_list_eval_requests` para atravessar exclusivamente `contains_pii`, `CsvClientRepository.list_all()` e `CsvLimitRequestRepository.list_all()`.
- **Testes (*replace, don't layer*):**
  - `tests/test_privacy.py`: cobertura direta para `contains_pii` (CPF e data de nascimento em múltiplos formatos, normalização e falha rápida com `DomainValidationError`, datas operacionais, datas inválidas no texto, coleções vazias e exclusão de credenciais) e verificação de privacidade de `_CPF_PATTERN`, `_DATE_PATTERN` e `_SECRET_PATTERN`.
  - `tests/test_csv_repositories.py`: cobertura para `CsvClientRepository.list_all()` e `CsvLimitRequestRepository.list_all()` (arquivo ausente, `0` bytes, cabeçalho vazio, múltiplos registros após `append()`, CSV corrompido, falha de `stat()` e symlink circular).
  - `tests/test_evals.py`: removidas as chamadas diretas a `runner._seed_pii`, mantendo a verificação ponta a ponta via interface pública (`main`/`run_evals`).

## Revisão
- `/code-review` em dois eixos (Standards e Spec) sobre `main...bc80495`: 0 violações de padrões documentados em Standards, 0 lacunas/desvios/escopo extra em Spec e 3 smells de baseline em Standards.
- Corrigidos os 2 smells aplicáveis em `4b8219e`: extração de `CsvLimitRequestRepository._is_empty()` unificando a verificação de arquivo vazio/ausente entre `list_all()` e `append()`, e renomeação de `_requests` para `_list_eval_requests(repository)` em `src/banco_agil/evals/runner.py`. O nome público `contains_pii` foi mantido integralmente por ser contrato explícito de `plan.md`.

## Verificação
- `make check`: `ruff check`, `ruff format --check`, `mypy src` (`--strict`, 35 arquivos) e 506 testes passando (99% cobertura total; 100% em `infrastructure/privacy.py` e `infrastructure/csv_repositories.py`; 99% em `evals/runner.py`).
- `make evals`: 12/12 cenários aprovados (100%), rotas 100%, 0 vazamentos de PII.

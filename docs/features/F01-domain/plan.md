# Plano — F01: Domínio e Persistência CSV

## Objetivo
Implementar a camada de domínio determinístico e os repositórios CSV do Banco Ágil (com TDD e zero dependências de LLM/rede), estabelecendo os contratos de dados, validações, fórmula de score, regra de aprovação em degraus e persistência segura que os agentes consumirão nas próximas features.

## Dependências a Adicionar
- `pydantic>=2.10.0` em `pyproject.toml` (atualizar `uv.lock` via `uv sync`).

## Escopo

### 1. Dados Seed (`data/`)
Calibrados para suportar testes manuais e a jornada completa (`rejeitado` → `entrevista` → `aprovado`):
- `data/score_limite.csv` (modelo em degraus `score_minimo,limite_maximo`):
  - `0 → 1000.00`, `300 → 3000.00`, `500 → 7000.00`, `650 → 15000.00`, `850 → 30000.00`
- `data/clientes.csv` (`cpf,nome,data_nascimento,limite_atual,score`):
  - `12345678901,Ana Souza,1990-05-14,2500.00,420` *(pede R$ 6.000 → rejeitado; entrevista eleva score ≥ 500 → aprovado)*
  - `98765432100,Carlos Mendes,1985-11-22,5000.00,720` *(aprovação direta até R$ 15.000)*
  - `45678912399,Mariana Costa,1998-02-03,800.00,220` *(score baixo)*
  - `11122233344,Roberto Almeida,1979-08-30,12000.00,890` *(faixa máxima)*
- `data/solicitacoes_aumento_limite.csv`: apenas o cabeçalho das 5 colunas (`cpf_cliente,data_hora_solicitacao,limite_atual,novo_limite_solicitado,status_pedido`).

### 2. Camada de Domínio (`src/banco_agil/domain/`)
- `errors.py`: hierarquia `DomainError`, `DomainValidationError`, `ClientNotFoundError`, `PersistenceError`.
- `models.py`:
  - `LimitIncreaseStatus(StrEnum)`: `PENDENTE = "pendente"`, `APROVADO = "aprovado"`, `REJEITADO = "rejeitado"`.
  - `EmploymentType(StrEnum)`: `FORMAL = "formal"`, `AUTONOMO = "autônomo"`, `DESEMPREGADO = "desempregado"`.
  - Modelos Pydantic v2 (`ConfigDict(frozen=True)` onde aplicável): `Client`, `ScoreLimitTier`, `LimitIncreaseRequest`, `LimitEvaluationResult`.
- `rules.py`:
  - Constantes de peso: `PESO_RENDA`, `PESO_EMPREGO`, `PESO_DEPENDENTES`, `PESO_DIVIDAS`.
  - Normalizadores estritos: `normalize_cpf`, `normalize_birth_date` (aceita `DD/MM/AAAA`, `AAAA-MM-DD`, `DD-MM-AAAA`, `DD.MM.AAAA`, rejeita datas inválidas/futuras), `normalize_employment` (aceita variações sem acento e sinônimos comuns como `clt`/`pj`), `normalize_dependents` (rejeita `bool` e negativos, mapeia `>= 3` para `"3+"`), `normalize_debts` (`sim`/`não` + variações).
  - `calculate_credit_score(...) -> int`: valida valores finitos e `>= 0`, aplica a fórmula da spec com `round` e clamp `[0, 1000]`.
  - `resolve_max_limit(score: int, tiers: Sequence[ScoreLimitTier]) -> float`: seleciona a linha com maior `score_minimo <= score` (lança `DomainError` se não houver faixa compatível).
  - `evaluate_limit_increase(client: Client, requested_limit: float, tiers: Sequence[ScoreLimitTier], now: datetime | None = None) -> LimitEvaluationResult`: valida `requested_limit` finito e estritamente `> client.limite_atual`; cria o pedido no domínio em `PENDENTE` com timestamp ISO 8601 com timezone e transita para `APROVADO` ou `REJEITADO`.
- `repositories.py`: interfaces `Protocol` desacoplando domínio da persistência (`ClientRepository`, `ScoreLimitRepository`, `LimitRequestRepository`).
- `services.py`: `CreditService` orquestrando `authenticate`, `get_client`, `process_limit_increase` (persiste pedido append-only e atualiza `limite_atual` em `clientes.csv` apenas se aprovado) e `recalculate_score` (calcula score e atualiza `clientes.csv`).

### 3. Camada de Infraestrutura (`src/banco_agil/infrastructure/`)
- `csv_repositories.py`: `CsvClientRepository`, `CsvScoreLimitRepository`, `CsvLimitRequestRepository` usando `csv` da stdlib.
  - Atualizações em `clientes.csv` usam escrita atômica (arquivo temporário no mesmo diretório + `os.replace`).
  - `CsvLimitRequestRepository.append` é append-only (criando cabeçalho automaticamente se o arquivo estiver vazio ou ausente).
  - Falhas de I/O ou CSV corrompido levantam `PersistenceError` sem vazar PII nas mensagens de erro.

### 4. Documentação Arquitetural
- `docs/adr/001-dominio-e-persistencia-csv.md` (seguindo `docs/adr/000-template.md`): registra separação domínio/repositórios (`Protocol`), modelo em degraus de `score_limite.csv`, `clientes.csv` como fonte da verdade, append-only de solicitações, clamp da fórmula de score e caminhos de evolução para escala.

## Fora de Escopo
- Não editar `docs/backlog.md` nem `AGENTS.md` (atualizados apenas no fechamento/integração na `main`).
- Sem modelos/clientes de câmbio (`F06`), sem LangGraph/LiteLLM/Streamlit (`F02`), sem mascaramento global de logging (`F07`).
- Sem validação de dígitos verificadores do CPF (apenas 11 dígitos normalizados, conforme `docs/spec.md`).

## Estratégia de Testes (TDD)
- `tests/test_domain_rules.py`:
  - Normalização de CPF (com/sem pontuação, tamanho inválido, vazio).
  - Normalização de datas (`DD/MM/AAAA`, `AAAA-MM-DD`, formato inválido, data inexistente, data futura).
  - Fórmula de score: casos típicos, normalização de emprego/dependentes (`bool`, negativo, `3+`, `>=3`)/dívidas, borda inferior (`-70 → 0`), borda superior (`despesas=0 → 1000`), `NaN`/`inf`/negativos.
  - `resolve_max_limit`: fronteiras exatas dos degraus (`0`, `299`, `300`, `499`, `500`, `850`, `1000`), tabela fora de ordem, tabela vazia/sem faixa.
  - `evaluate_limit_increase`: aprovado, rejeitado, `requested_limit <= limite_atual` (lança `DomainValidationError`), `requested_limit <= 0` ou `NaN`.
- `tests/test_csv_repositories.py` (100% isolado via `tmp_path`):
  - Leitura e autenticação (sucesso, senha/data errada, CPF inexistente, formatos alternativos).
  - Atualização atômica de `limite_atual` e `score` preservando os demais clientes.
  - Append-only de solicitações (inclusive criação automática do header) com validação das 5 colunas e formato ISO 8601.
  - Tratamento de erros de I/O / CSV ausente ou malformado → `PersistenceError` (sem expor CPF na mensagem).
- `tests/test_credit_service.py`:
  - Fluxo aprovado (grava pedido `aprovado` + atualiza `clientes.csv`).
  - Fluxo rejeitado (grava pedido `rejeitado` + mantém `clientes.csv` inalterado).
  - Fluxo rejeitado → `recalculate_score` → nova avaliação aprovada.
  - Teste somente-leitura nos CSVs reais de `data/` validando o contrato dos seeds e que o cenário da cliente Ana Souza é reproduzível.

## Critérios de Aceite
- [ ] `make check` 100% verde (`ruff check`, `ruff format --check`, `mypy src` strict, `pytest --cov=banco_agil`).
- [ ] Cobertura ≥ 85% nos módulos de `src/banco_agil/domain/` e `src/banco_agil/infrastructure/`.
- [ ] Nenhum dado pessoal (CPF/data de nascimento) incluído em mensagens de exceção ou logs.
- [ ] Commits pequenos em Conventional Commits dentro da branch `feat/f01-domain`.
- [ ] `docs/adr/001-dominio-e-persistencia-csv.md` criado e revisado.

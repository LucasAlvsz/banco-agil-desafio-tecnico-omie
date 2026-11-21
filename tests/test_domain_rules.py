from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from banco_agil.domain.errors import DomainError, DomainValidationError
from banco_agil.domain.models import Client, ScoreLimitTier
from banco_agil.domain.rules import (
    calculate_credit_score,
    evaluate_limit_increase,
    normalize_birth_date,
    normalize_cpf,
    normalize_debts,
    normalize_dependents,
    normalize_employment,
    resolve_max_limit,
)


@pytest.fixture
def client():
    return Client(
        cpf="12345678901", nome="Ana", data_nascimento="1990-05-14", limite_atual=2500, score=420
    )


@pytest.fixture
def tiers():
    return [
        ScoreLimitTier(score_minimo=s, limite_maximo=v)
        for s, v in [(850, 30000), (0, 1000), (500, 7000), (300, 3000), (650, 15000)]
    ]


@pytest.mark.parametrize("value", ["12345678901", "123.456.789-01"])
def test_cpf(value):
    assert normalize_cpf(value) == "12345678901"


@pytest.mark.parametrize("value", ["", "123", "abc12345678901", "１２３４５６７８９０１", None])
def test_invalid_cpf(value):
    with pytest.raises(DomainValidationError):
        normalize_cpf(value)


@pytest.mark.parametrize("value", ["14/05/1990", "1990-05-14", "14-05-1990", "14.05.1990"])
def test_birth_date(value):
    assert normalize_birth_date(value) == "1990-05-14"


@pytest.mark.parametrize("value", ["31/02/2000", "2999-01-01", "", "1990/05/14", None])
def test_invalid_date(value):
    with pytest.raises(DomainValidationError):
        normalize_birth_date(value)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("CLT", "formal"),
        ("pj", "autônomo"),
        ("autonomo", "autônomo"),
        ("desempregado", "desempregado"),
    ],
)
def test_employment(value, expected):
    assert normalize_employment(value) == expected


@pytest.mark.parametrize("value, expected", [(0, 0), ("2", 2), (3, "3+"), (20, "3+"), ("3+", "3+")])
def test_dependents(value, expected):
    assert normalize_dependents(value) == expected


@pytest.mark.parametrize("value", [True, -1, "abc", 1.5])
def test_invalid_dependents(value):
    with pytest.raises(DomainValidationError):
        normalize_dependents(value)


@pytest.mark.parametrize(
    "value, expected", [("SIM", "sim"), ("nao", "não"), (True, "sim"), (False, "não")]
)
def test_debts(value, expected):
    assert normalize_debts(value) == expected


@pytest.mark.parametrize("normalizer", [normalize_debts, normalize_employment])
def test_unknown_category(normalizer):
    with pytest.raises(DomainValidationError):
        normalizer("unknown")


@pytest.mark.parametrize(
    "income, expenses, job, deps, debts, expected",
    [
        (5000, 2000, "clt", 0, "não", 575),
        (0, 0, "desempregado", 3, "sim", 0),
        (5000, 0, "pj", 2, "sim", 1000),
    ],
)
def test_score(income, expenses, job, deps, debts, expected):
    assert calculate_credit_score(income, expenses, job, deps, debts) == expected


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
@pytest.mark.parametrize("field", [0, 1])
def test_invalid_money(value, field):
    args = [1000, 1000, "clt", 0, "não"]
    args[field] = value
    with pytest.raises(DomainValidationError):
        calculate_credit_score(*args)


@pytest.mark.parametrize(
    "score, expected",
    [(0, 1000), (299, 1000), (300, 3000), (499, 3000), (500, 7000), (850, 30000), (1000, 30000)],
)
def test_tiers(score, expected, tiers):
    assert resolve_max_limit(score, tiers) == expected


def test_missing_tier():
    for tiers in [[], [ScoreLimitTier(score_minimo=500, limite_maximo=7000)]]:
        with pytest.raises(DomainError):
            resolve_max_limit(0, tiers)


@pytest.mark.parametrize("requested, status", [(3000, "aprovado"), (6000, "rejeitado")])
def test_evaluation(client, tiers, requested, status):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = evaluate_limit_increase(client, requested, tiers, now)
    assert result.pedido.status_pedido == status
    assert datetime.fromisoformat(result.pedido.data_hora_solicitacao) == now
    assert result.limite_maximo == 3000


@pytest.mark.parametrize("requested", [2500, 0, -1, float("nan"), float("inf")])
def test_invalid_request(client, tiers, requested):
    with pytest.raises(DomainValidationError):
        evaluate_limit_increase(client, requested, tiers)


def test_model_validation_hides_pii(client):
    with pytest.raises(ValidationError) as error:
        Client(**(client.model_dump() | {"score": -1, "cpf": "private-cpf"}))
    assert "private-cpf" not in str(error.value)
    with pytest.raises(ValidationError):
        client.score = 500


def test_naive_evaluation_timestamp(client, tiers):
    with pytest.raises(DomainValidationError):
        evaluate_limit_increase(client, 6000, tiers, datetime(2026, 1, 1))


def test_request_requires_timezone():
    from banco_agil.domain.models import LimitIncreaseRequest

    with pytest.raises(ValidationError):
        LimitIncreaseRequest(
            cpf_cliente="12345678901",
            data_hora_solicitacao="2026-01-01T00:00:00",
            limite_atual=2500,
            novo_limite_solicitado=6000,
        )

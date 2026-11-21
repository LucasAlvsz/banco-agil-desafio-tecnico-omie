import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from banco_agil.domain.enums import EmploymentType
from banco_agil.domain.errors import DomainError, DomainValidationError

if TYPE_CHECKING:
    from banco_agil.domain.models import Client, LimitEvaluationResult, ScoreLimitTier

PESO_RENDA = 30
PESO_EMPREGO: dict[EmploymentType, int] = {
    EmploymentType.FORMAL: 300,
    EmploymentType.AUTONOMO: 200,
    EmploymentType.DESEMPREGADO: 0,
}
PESO_DEPENDENTES = {0: 100, 1: 80, 2: 60, "3+": 30}
PESO_DIVIDAS = {"sim": -100, "não": 100}
CURRENCY_ALIASES = {
    "dolar": "USD",
    "dolar americano": "USD",
    "us$": "USD",
    "euro": "EUR",
    "libra": "GBP",
    "libra esterlina": "GBP",
    "iene": "JPY",
    "franco": "CHF",
    "franco suico": "CHF",
    "peso argentino": "ARS",
    "dolar canadense": "CAD",
    "dolar australiano": "AUD",
    "yuan": "CNY",
    "renminbi": "CNY",
    "bitcoin": "BTC",
}


def normalize_cpf(value: object) -> str:
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"[0-9.\-\s]+", value):
            digits = re.sub(r"[^0-9]", "", value)
            if len(digits) == 11:
                return digits
    raise DomainValidationError("CPF inválido")


def normalize_birth_date(value: object) -> str:
    if isinstance(value, str):
        value = value.strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(value, fmt).date()
            except ValueError:
                continue
            if parsed <= date.today():
                return parsed.isoformat()
    raise DomainValidationError("Data de nascimento inválida")


def _text(value: object) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFD", str(value).strip().lower())
        if unicodedata.category(c) != "Mn"
    )


def normalize_currency_code(value: object) -> str:
    if isinstance(value, str):
        original = " ".join(value.split())
        text = _text(original)
        if text in {"brl", "real", "reais"}:
            raise DomainValidationError("O real (BRL) já é a moeda de referência.")
        if text in CURRENCY_ALIASES:
            return CURRENCY_ALIASES[text]
        if re.fullmatch(r"[A-Za-z]{3}", original):
            return original.upper()
    raise DomainValidationError("Informe um nome de moeda conhecido ou um código de três letras.")


def normalize_employment(value: object) -> EmploymentType:
    aliases = {
        "clt": "formal",
        "formal": "formal",
        "pj": "autônomo",
        "autonomo": "autônomo",
        "desempregado": "desempregado",
    }
    try:
        return EmploymentType(aliases[_text(value)])
    except KeyError:
        raise DomainValidationError("Tipo de emprego inválido") from None


def normalize_dependents(value: object) -> int | str:
    if isinstance(value, str):
        value = value.strip()
    if value == "3+":
        return "3+"
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise DomainValidationError("Dependentes inválidos")
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]+", value):
            raise DomainValidationError("Dependentes inválidos")
        value = int(value)
    if value < 0:
        raise DomainValidationError("Dependentes inválidos")
    return "3+" if value >= 3 else value


def normalize_debts(value: object) -> str:
    if isinstance(value, bool):
        return "sim" if value else "não"
    aliases = {"sim": "sim", "s": "sim", "nao": "não", "n": "não"}
    try:
        return aliases[_text(value)]
    except KeyError:
        raise DomainValidationError("Dívidas inválidas") from None


def _validate_money(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise DomainValidationError("Valor monetário inválido")


def calculate_credit_score(
    *,
    renda_mensal: float,
    despesas_fixas: float,
    tipo_emprego: object,
    num_dependentes: object,
    tem_dividas: object,
) -> int:
    _validate_money(renda_mensal)
    _validate_money(despesas_fixas)
    raw = (renda_mensal / (despesas_fixas + 1)) * PESO_RENDA
    raw += PESO_EMPREGO[normalize_employment(tipo_emprego)]
    raw += PESO_DEPENDENTES[normalize_dependents(num_dependentes)]
    raw += PESO_DIVIDAS[normalize_debts(tem_dividas)]
    return round(max(0, min(1000, raw)))


def resolve_max_limit(score: int, tiers: Sequence["ScoreLimitTier"]) -> float:
    eligible = [tier for tier in tiers if tier.score_minimo <= score]
    if not eligible:
        raise DomainError("Faixa de score indisponível")
    return max(eligible, key=lambda tier: tier.score_minimo).limite_maximo


def evaluate_limit_increase(
    client: "Client",
    requested_limit: float,
    tiers: Sequence["ScoreLimitTier"],
    now: datetime | None = None,
) -> "LimitEvaluationResult":
    from banco_agil.domain.models import (
        LimitEvaluationResult,
        LimitIncreaseRequest,
        LimitIncreaseStatus,
    )

    _validate_money(requested_limit)
    if requested_limit <= client.limite_atual:
        raise DomainValidationError("O novo limite deve superar o limite atual")
    timestamp = now if now is not None else datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DomainValidationError("Timestamp deve incluir timezone")
    maximum = resolve_max_limit(client.score, tiers)
    request = LimitIncreaseRequest(
        cpf_cliente=client.cpf,
        data_hora_solicitacao=timestamp.isoformat(),
        limite_atual=client.limite_atual,
        novo_limite_solicitado=requested_limit,
    )
    status = (
        LimitIncreaseStatus.APROVADO
        if requested_limit <= maximum
        else LimitIncreaseStatus.REJEITADO
    )
    return LimitEvaluationResult(
        pedido=request.model_copy(update={"status_pedido": status}), limite_maximo=maximum
    )

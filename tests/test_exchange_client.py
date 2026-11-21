import logging

import httpx
import pytest
from pydantic import ValidationError

from banco_agil.config import Settings
from banco_agil.domain.errors import DomainValidationError, ExchangeServiceError
from banco_agil.domain.models import ExchangeQuote
from banco_agil.domain.rules import normalize_currency_code
from banco_agil.infrastructure.exchange_client import ExchangeRateClient

AWESOME_URL = "https://economia.awesomeapi.com.br/json/last/USD-BRL"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=BRL"
LOGGER = "banco_agil.infrastructure.exchange_client"
PAIR = {"name": "Dólar Americano/Real Brasileiro", "create_date": "2026-11-21 10:30:00"}
REFERENCE = {"rates": {"BRL": 5.17}, "date": "2026-11-20"}


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (" DÓLAR ", "USD"),
        ("dolar", "USD"),
        ("dólar   americano", "USD"),
        ("US$", "USD"),
        ("Euro", "EUR"),
        ("LIBRA", "GBP"),
        ("libra esterlina", "GBP"),
        ("iene", "JPY"),
        ("franco", "CHF"),
        ("franco suíço", "CHF"),
        ("peso argentino", "ARS"),
        ("dólar canadense", "CAD"),
        ("dólar australiano", "AUD"),
        ("yuan", "CNY"),
        ("renminbi", "CNY"),
        ("bitcoin", "BTC"),
        (" mxn ", "MXN"),
    ],
)
def test_normalize_currency_names_and_codes(value, code):
    assert normalize_currency_code(value) == code


@pytest.mark.parametrize("value", ["BRL", " real ", "REAIS"])
def test_reject_reference_currency(value):
    with pytest.raises(DomainValidationError, match="já é a moeda de referência"):
        normalize_currency_code(value)


@pytest.mark.parametrize("value", [" ", "", None, 21, "UŚD", "UſD", "123.456.789-01"])
def test_reject_invalid_currency_without_echoing_input(value):
    with pytest.raises(DomainValidationError) as error:
        normalize_currency_code(value)
    assert str(error.value) == "Informe um nome de moeda conhecido ou um código de três letras."


def test_exchange_timeout_setting(monkeypatch):
    assert Settings().exchange_timeout_seconds == 8.0
    monkeypatch.setenv("EXCHANGE_TIMEOUT_SECONDS", "2.5")
    assert Settings().exchange_timeout_seconds == 2.5
    with pytest.raises(ValidationError, match="exchange_timeout_seconds"):
        Settings(exchange_timeout_seconds=0)


async def test_invalid_currency_is_rejected_before_http():
    def respond(request):
        pytest.fail("Moeda inválida chegou ao transporte HTTP.")

    client = ExchangeRateClient(transport=httpx.MockTransport(respond))
    with pytest.raises(DomainValidationError, match="moeda de referência"):
        await client.get_quote("real")


@pytest.mark.parametrize("ask", [{}, {"ask": "5.2711"}])
async def test_primary_quote(ask):
    def respond(request):
        assert request.method == "GET"
        assert str(request.url) == AWESOME_URL
        assert set(request.extensions["timeout"].values()) == {2.5}
        return httpx.Response(200, json={"USDBRL": {**PAIR, "bid": "5.25", **ask}})

    client = ExchangeRateClient(timeout_seconds=2.5, transport=httpx.MockTransport(respond))
    quote = await client.get_quote()
    assert quote == ExchangeQuote(
        currency_code="USD",
        currency_name="Dólar Americano",
        bid=5.25,
        ask=5.2711 if ask else None,
        timestamp="2026-11-21 10:30:00",
        source="AwesomeAPI",
    )
    assert quote.target_currency == "BRL"
    assert not quote.is_reference_rate
    with pytest.raises(ValidationError, match="frozen"):
        quote.bid = 6.0


@pytest.mark.parametrize(
    "primary_failure",
    [
        httpx.ReadTimeout("detalhe sigiloso"),
        httpx.ConnectError("detalhe sigiloso"),
        httpx.Response(500),
        httpx.Response(404),
        httpx.Response(200, content=b"{"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"USDBRL": []}),
        httpx.Response(200, json={"USDBRL": PAIR}),
        httpx.Response(200, json={"USDBRL": {**PAIR, "bid": "0"}}),
        httpx.Response(200, json={"USDBRL": {**PAIR, "bid": "NaN"}}),
        httpx.Response(200, json={"USDBRL": {**PAIR, "bid": "inf"}}),
        httpx.Response(200, json={"USDBRL": {**PAIR, "bid": True}}),
        httpx.Response(200, json={"USDBRL": {**PAIR, "bid": "5.25", "ask": "-1"}}),
        httpx.Response(200, json={"USDBRL": {**PAIR, "bid": "5.25", "ask": None}}),
    ],
)
async def test_failed_primary_returns_frankfurter_reference(primary_failure, caplog):
    urls = []

    def respond(request):
        urls.append(str(request.url))
        if request.url.host == "economia.awesomeapi.com.br":
            if isinstance(primary_failure, httpx.HTTPError):
                raise primary_failure
            return primary_failure
        assert str(request.url) == FRANKFURTER_URL
        return httpx.Response(200, json=REFERENCE)

    client = ExchangeRateClient(transport=httpx.MockTransport(respond))
    quote = await client.get_quote("dólar")
    assert urls == [AWESOME_URL, FRANKFURTER_URL]
    assert quote.currency_code == "USD"
    assert quote.source == "Frankfurter (BCE)"
    assert quote.bid == 5.17
    assert quote.ask is None
    assert quote.timestamp == "2026-11-20"
    assert quote.is_reference_rate
    [warning] = [record for record in caplog.records if record.name == LOGGER]
    assert warning.levelno == logging.WARNING
    assert warning.args[0] == "USD"
    assert "detalhe sigiloso" not in caplog.text


@pytest.mark.parametrize(
    ("reference_failure", "error_type"),
    [
        (httpx.Response(503, text="detalhe sigiloso"), "HTTPStatusError"),
        (httpx.Response(200, json={}), "KeyError"),
        (httpx.Response(200, json={**REFERENCE, "rates": {}}), "KeyError"),
        (httpx.Response(200, json={**REFERENCE, "rates": {"BRL": -1}}), "ValidationError"),
        (httpx.Response(200, json={**REFERENCE, "rates": {"BRL": "NaN"}}), "ValidationError"),
        (httpx.Response(200, json={**REFERENCE, "date": "2026-11-31"}), "ValueError"),
        (httpx.Response(200, json={**REFERENCE, "date": "20261120"}), "ValueError"),
        (httpx.Response(200, json={**REFERENCE, "date": None}), "ValueError"),
    ],
)
async def test_total_failure_is_controlled_and_logs_only_safe_fields(
    reference_failure, error_type, caplog
):
    def respond(request):
        if str(request.url) == AWESOME_URL:
            raise httpx.ReadTimeout("detalhe sigiloso")
        assert str(request.url) == FRANKFURTER_URL
        return reference_failure

    client = ExchangeRateClient(transport=httpx.MockTransport(respond))
    with pytest.raises(ExchangeServiceError) as error:
        await client.get_quote()
    assert str(error.value) == "Não foi possível consultar a cotação no momento."
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    assert [
        (record.levelno, record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == LOGGER
    ] == [
        (logging.WARNING, "AwesomeAPI indisponível para USD (ReadTimeout)", None),
        (logging.ERROR, f"Falha na consulta de câmbio para USD ({error_type})", None),
    ]
    assert "detalhe sigiloso" not in caplog.text

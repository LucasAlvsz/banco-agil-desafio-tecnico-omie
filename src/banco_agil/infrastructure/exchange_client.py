import logging
from datetime import date
from typing import Any

import httpx

from banco_agil.domain.errors import ExchangeServiceError
from banco_agil.domain.models import ExchangeQuote
from banco_agil.domain.rules import normalize_currency_code

logger = logging.getLogger(__name__)

AWESOME_URL = "https://economia.awesomeapi.com.br/json/last"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Objeto JSON esperado")
    return value


def _rate(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Taxa numérica esperada")
    return float(value)


class ExchangeRateClient:
    def __init__(
        self, timeout_seconds: float = 8.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout_seconds
        self._transport = transport

    async def get_quote(self, currency: str = "USD") -> ExchangeQuote:
        code = normalize_currency_code(currency)
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.get(f"{AWESOME_URL}/{code}-BRL")
                payload = _object(response.raise_for_status().json())
                pair = _object(payload[f"{code}BRL"])
                name = pair.get("name", code)
                return ExchangeQuote(
                    currency_code=code,
                    currency_name=name.split("/", 1)[0].strip() if isinstance(name, str) else name,
                    bid=_rate(pair["bid"]),
                    ask=_rate(pair["ask"]) if "ask" in pair else None,
                    timestamp=pair["create_date"],
                    source="AwesomeAPI",
                )
            except (httpx.HTTPError, KeyError, ValueError, OverflowError) as exc:
                logger.warning("AwesomeAPI indisponível para %s (%s)", code, type(exc).__name__)

            try:
                response = await client.get(
                    FRANKFURTER_URL, params={"base": code, "symbols": "BRL"}
                )
                payload = _object(response.raise_for_status().json())
                timestamp = payload["date"]
                if (
                    not isinstance(timestamp, str)
                    or date.fromisoformat(timestamp).isoformat() != timestamp
                ):
                    raise ValueError("Data de referência inválida")
                return ExchangeQuote(
                    currency_code=code,
                    currency_name=code,
                    bid=_rate(_object(payload["rates"])["BRL"]),
                    timestamp=timestamp,
                    source="Frankfurter (BCE)",
                    is_reference_rate=True,
                )
            except (httpx.HTTPError, KeyError, ValueError, OverflowError) as exc:
                logger.error("Falha na consulta de câmbio para %s (%s)", code, type(exc).__name__)
                raise ExchangeServiceError(
                    "Não foi possível consultar a cotação no momento."
                ) from None

from typing import Protocol

from banco_agil.domain.models import Client, ExchangeQuote, LimitIncreaseRequest, ScoreLimitTier


class ClientRepository(Protocol):
    def get(self, cpf: str) -> Client: ...
    def update(self, client: Client) -> None: ...


class ScoreLimitRepository(Protocol):
    def list_all(self) -> list[ScoreLimitTier]: ...


class LimitRequestRepository(Protocol):
    def append(self, request: LimitIncreaseRequest) -> None: ...


class ExchangeRateProvider(Protocol):
    async def get_quote(self, currency: str = "USD") -> ExchangeQuote: ...

import csv
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from banco_agil.domain.errors import ClientNotFoundError, PersistenceError
from banco_agil.domain.models import Client, DomainModel, LimitIncreaseRequest, ScoreLimitTier
from banco_agil.domain.rules import normalize_cpf


def _read[Model: DomainModel](path: Path, model: type[Model]) -> list[Model]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(model.model_fields):
                raise ValueError
            rows = []
            for row in reader:
                if None in row or None in row.values():
                    raise ValueError
                rows.append(model.model_validate(row))
            return rows
    except (OSError, UnicodeError, csv.Error, ValueError, ValidationError):
        raise PersistenceError("Não foi possível ler os dados CSV") from None


class CsvClientRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list_all(self) -> list[Client]:
        clients = _read(self.path, Client)
        if len({client.cpf for client in clients}) != len(clients):
            raise PersistenceError("Cadastro CSV duplicado")
        return clients

    def get(self, cpf: str) -> Client:
        cpf = normalize_cpf(cpf)
        for client in self.list_all():
            if client.cpf == cpf:
                return client
        raise ClientNotFoundError("Cliente não encontrado")

    def update(self, client: Client) -> None:
        clients = self.list_all()
        if not any(existing.cpf == client.cpf for existing in clients):
            raise ClientNotFoundError("Cliente não encontrado")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", newline="", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                writer = csv.DictWriter(stream, fieldnames=list(Client.model_fields))
                writer.writeheader()
                for existing in clients:
                    writer.writerow(
                        (client if existing.cpf == client.cpf else existing).model_dump()
                    )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, csv.Error):
            raise PersistenceError("Não foi possível atualizar os dados CSV") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class CsvScoreLimitRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list_all(self) -> list[ScoreLimitTier]:
        tiers = _read(self.path, ScoreLimitTier)
        if len({tier.score_minimo for tier in tiers}) != len(tiers):
            raise PersistenceError("Degrau de score CSV duplicado")
        return tiers


class CsvLimitRequestRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list_all(self) -> list[LimitIncreaseRequest]:
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return []
        except OSError:
            raise PersistenceError("Não foi possível ler os dados CSV") from None
        return _read(self.path, LimitIncreaseRequest)

    def append(self, request: LimitIncreaseRequest) -> None:
        try:
            empty = not self.path.exists() or self.path.stat().st_size == 0
            if not empty:
                _read(self.path, LimitIncreaseRequest)
            with self.path.open("a", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(LimitIncreaseRequest.model_fields))
                if empty:
                    writer.writeheader()
                writer.writerow(request.model_dump(mode="json"))
        except (OSError, csv.Error):
            raise PersistenceError("Não foi possível registrar o pedido CSV") from None

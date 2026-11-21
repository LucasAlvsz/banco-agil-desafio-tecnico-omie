import csv
from pathlib import Path

import pytest
from banco_agil.domain.services import CreditService
from banco_agil.infrastructure.csv_repositories import (
    CsvClientRepository,
    CsvLimitRequestRepository,
    CsvScoreLimitRepository,
)

from banco_agil.domain.errors import DomainValidationError

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture
def service(tmp_path):
    for path in DATA.glob("*.csv"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    return CreditService(
        CsvClientRepository(tmp_path / "clientes.csv"),
        CsvScoreLimitRepository(tmp_path / "score_limite.csv"),
        CsvLimitRequestRepository(tmp_path / "solicitacoes_aumento_limite.csv"),
    )


def test_authentication(service):
    assert service.authenticate("123.456.789-01", "14/05/1990").nome == "Ana Souza"
    assert service.authenticate("12345678901", "1990-05-15") is None
    assert service.authenticate("11111111111", "1990-05-14") is None
    with pytest.raises(DomainValidationError):
        service.authenticate("bad", "bad")


def test_rejection_interview_approval(service, tmp_path):
    clients = tmp_path / "clientes.csv"
    before = clients.read_bytes()
    assert service.process_limit_increase("12345678901", 6000).pedido.status_pedido == "rejeitado"
    assert clients.read_bytes() == before
    updated = service.recalculate_score("12345678901", 5000, 2000, "clt", 0, "não")
    assert updated.score == 575
    assert service.process_limit_increase("12345678901", 6000).pedido.status_pedido == "aprovado"
    assert service.get_client("12345678901").limite_atual == 6000
    with (tmp_path / "solicitacoes_aumento_limite.csv").open() as stream:
        assert [r["status_pedido"] for r in csv.DictReader(stream)] == ["rejeitado", "aprovado"]


def test_direct_approval_and_invalid_request(service, tmp_path):
    assert service.process_limit_increase("98765432100", 15000).pedido.status_pedido == "aprovado"
    path = tmp_path / "solicitacoes_aumento_limite.csv"
    before = path.read_bytes()
    with pytest.raises(DomainValidationError):
        service.process_limit_increase("98765432100", 15000)
    assert path.read_bytes() == before


def test_real_seeds_read_only():
    from banco_agil.domain.rules import calculate_credit_score, evaluate_limit_increase

    clients = CsvClientRepository(DATA / "clientes.csv")
    tiers = CsvScoreLimitRepository(DATA / "score_limite.csv").list_all()
    assert [(t.score_minimo, t.limite_maximo) for t in tiers] == [
        (0, 1000),
        (300, 3000),
        (500, 7000),
        (650, 15000),
        (850, 30000),
    ]
    ana = clients.get("12345678901")
    assert evaluate_limit_increase(ana, 6000, tiers).pedido.status_pedido == "rejeitado"
    score = calculate_credit_score(5000, 2000, "clt", 0, "não")
    assert (
        evaluate_limit_increase(
            ana.model_copy(update={"score": score}), 6000, tiers
        ).pedido.status_pedido
        == "aprovado"
    )
    with (DATA / "clientes.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 4
    with (DATA / "solicitacoes_aumento_limite.csv").open() as stream:
        reader = csv.DictReader(stream)
        assert len(reader.fieldnames) == 5
        assert list(reader) == []

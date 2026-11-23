import csv
from pathlib import Path
from unittest.mock import patch

import pytest

from banco_agil.domain.errors import ClientNotFoundError, PersistenceError
from banco_agil.domain.models import Client
from banco_agil.infrastructure.csv_repositories import (
    CsvClientRepository,
    CsvLimitRequestRepository,
    CsvScoreLimitRepository,
)

LIMIT_REQUEST_HEADER = (
    "cpf_cliente,data_hora_solicitacao,limite_atual,novo_limite_solicitado,status_pedido\n"
)


@pytest.fixture
def clients(tmp_path):
    path = tmp_path / "clientes.csv"
    path.write_text(
        "cpf,nome,data_nascimento,limite_atual,score\n"
        "12345678901,Ana,1990-05-14,2500.00,420\n"
        "98765432100,Carlos,1985-11-22,5000.00,720\n"
    )
    return CsvClientRepository(path)


def test_lists_all_clients_as_domain_models(clients):
    ana, carlos = clients.list_all()
    assert isinstance(ana, Client)
    assert isinstance(carlos, Client)
    assert (ana.cpf, carlos.cpf) == ("12345678901", "98765432100")


def test_client_update(clients):
    before = clients.get("98765432100")
    ana = clients.get("12345678901")
    clients.update(ana.model_copy(update={"score": 575, "limite_atual": 6000}))
    assert clients.get("12345678901").score == 575
    assert clients.get("12345678901").limite_atual == 6000
    assert clients.get("98765432100") == before
    with pytest.raises(ClientNotFoundError):
        clients.get("11111111111")
    with pytest.raises(ClientNotFoundError):
        clients.update(ana.model_copy(update={"cpf": "11111111111"}))


def test_atomic_failure(clients):
    before = clients.path.read_bytes()
    with (
        patch("banco_agil.infrastructure.csv_repositories.os.replace", side_effect=OSError),
        pytest.raises(PersistenceError),
    ):
        clients.update(clients.get("12345678901").model_copy(update={"score": 500}))
    assert clients.path.read_bytes() == before
    assert list(clients.path.parent.iterdir()) == [clients.path]


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "",
        "wrong,header\n1,2\n",
        "cpf,nome,data_nascimento,limite_atual,score\n12345678901,Ana,1990-05-14,bad,420\n",
        "cpf,nome,data_nascimento,limite_atual,score\n12345678901,Ana\n",
        "cpf,nome,data_nascimento,limite_atual,score\n12345678901,Ana,1990-05-14,2500,420,extra\n",
    ],
)
def test_corrupt_clients(tmp_path, contents):
    path = tmp_path / "clients.csv"
    if contents is not None:
        path.write_text(contents)
    with pytest.raises(PersistenceError) as error:
        CsvClientRepository(path).list_all()
    assert "12345678901" not in str(error.value)
    assert error.value.__suppress_context__


def test_tiers(tmp_path):
    path = tmp_path / "tiers.csv"
    path.write_text("score_minimo,limite_maximo\n0,1000\n500,7000\n")
    assert len(CsvScoreLimitRepository(path).list_all()) == 2
    path.write_text("score_minimo,limite_maximo\nbad,1000\n")
    with pytest.raises(PersistenceError):
        CsvScoreLimitRepository(path).list_all()


@pytest.mark.parametrize("empty", [False, True])
def test_append(tmp_path, empty):
    from banco_agil.domain.models import ScoreLimitTier
    from banco_agil.domain.rules import evaluate_limit_increase

    path = tmp_path / "requests.csv"
    if empty:
        path.touch()
    repo = CsvLimitRequestRepository(path)
    client = Client(
        cpf="12345678901", nome="Ana", data_nascimento="1990-05-14", score=420, limite_atual=2500
    )
    request = evaluate_limit_increase(
        client, 6000, [ScoreLimitTier(score_minimo=0, limite_maximo=3000)]
    ).pedido
    repo.append(request)
    first = path.read_bytes()
    second_request = request.model_copy(update={"novo_limite_solicitado": 7000})
    repo.append(second_request)
    assert repo.list_all() == [request, second_request]
    assert path.read_bytes().startswith(first)
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert len(rows[0]) == 5
    assert rows[0]["status_pedido"] == "rejeitado"
    path.write_text("wrong,header\n")
    with pytest.raises(PersistenceError):
        repo.append(request)
    with patch.object(Path, "open", side_effect=OSError), pytest.raises(PersistenceError):
        repo.append(request)


def test_duplicate_clients(clients):
    with clients.path.open("a") as stream:
        stream.write("12345678901,Ana,1990-05-14,2500.00,420\n")
    with pytest.raises(PersistenceError, match="Cadastro CSV duplicado"):
        clients.list_all()


@pytest.mark.parametrize("contents", [None, "", LIMIT_REQUEST_HEADER])
def test_lists_no_requests_before_the_first_append(tmp_path, contents):
    path = tmp_path / "requests.csv"
    if contents is not None:
        path.write_text(contents)
    assert CsvLimitRequestRepository(path).list_all() == []


@pytest.mark.parametrize(
    "contents",
    [
        "wrong,header\n",
        LIMIT_REQUEST_HEADER + "12345678901,2026-01-01T00:00:00+00:00,2500,6000,wrong\n",
        LIMIT_REQUEST_HEADER + "12345678901,2026-01-01T00:00:00+00:00,2500,6000\n",
    ],
)
def test_rejects_corrupt_limit_request_csv(tmp_path, contents):
    path = tmp_path / "requests.csv"
    path.write_text(contents)
    with pytest.raises(PersistenceError, match="Não foi possível ler os dados CSV"):
        CsvLimitRequestRepository(path).list_all()


def test_lists_limit_requests_with_safe_error_on_stat_failure(tmp_path):
    path = tmp_path / "requests.csv"
    path.touch()
    repo = CsvLimitRequestRepository(path)
    with (
        patch.object(Path, "stat", side_effect=PermissionError("12345678901")),
        pytest.raises(PersistenceError, match="^Não foi possível ler os dados CSV$") as error,
    ):
        repo.list_all()
    assert error.value.__suppress_context__


def test_append_write_failure(tmp_path):
    from banco_agil.domain.models import LimitIncreaseRequest

    request = LimitIncreaseRequest(
        cpf_cliente="12345678901",
        data_hora_solicitacao="2026-01-01T00:00:00+00:00",
        limite_atual=2500,
        novo_limite_solicitado=6000,
    )
    repo = CsvLimitRequestRepository(tmp_path / "missing" / "requests.csv")
    with pytest.raises(PersistenceError):
        repo.append(request)


@pytest.mark.parametrize("limits", [(1000, 1000), (1000, 7000)])
def test_duplicate_score_tiers(tmp_path, limits):
    path = tmp_path / "tiers.csv"
    path.write_text(f"score_minimo,limite_maximo\n0,{limits[0]}\n0,{limits[1]}\n")
    with pytest.raises(PersistenceError):
        CsvScoreLimitRepository(path).list_all()

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from banco_agil.agents.graph import TurnResult
from banco_agil.config import Settings
from banco_agil.evals import runner
from banco_agil.evals.__main__ import main
from banco_agil.evals.models import EvalReport, EvalScenario
from banco_agil.evals.runner import DEFAULT_SCENARIOS_PATH, load_scenarios, run_evals
from banco_agil.infrastructure.observability import LangfuseObservability
from banco_agil.runtime import ConversationRuntime

DATA_DIR = Path("data")


@pytest.fixture
def single_scenario(tmp_path):
    def write(*, message="Você pode fazer Pix?", script=None, expect=None):
        sample = {
            "id": "sample",
            "turns": [
                {
                    "message": message,
                    "script": [] if script is None else script,
                    "expect": {
                        "active_agent": "triagem",
                        "route": ["triagem"],
                        "authenticated": False,
                    }
                    | (expect or {}),
                }
            ],
        }
        path = tmp_path / "scenarios.json"
        path.write_text(json.dumps([sample]), encoding="utf-8")
        return path

    return write


def test_dataset_and_offline_evals_are_complete_and_preserve_seeds(monkeypatch):
    def forbidden(_):
        pytest.fail("Modo offline não pode inicializar observabilidade externa")

    monkeypatch.setattr(runner, "create_langfuse_observability", forbidden)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    original = {path: path.read_bytes() for path in DATA_DIR.glob("*.csv")}
    scenarios = load_scenarios()
    assert len(scenarios) >= 12 and len(scenarios) == len({item.id for item in scenarios})
    report = run_evals(settings=Settings(data_dir=DATA_DIR))
    assert report.total_scenarios == report.passed_scenarios == len(scenarios)
    assert report.pass_rate == report.route_accuracy == 1.0
    assert report.failed_scenarios == report.pii_leaks == 0
    assert report.total_tokens > 0 and report.total_cost_usd > 0
    assert report.avg_latency_ms >= 0 and report.p95_latency_ms >= 0
    assert {path: path.read_bytes() for path in DATA_DIR.glob("*.csv")} == original


def test_load_scenarios_rejects_duplicate_ids_and_invalid_turns(tmp_path, single_scenario):
    path = single_scenario()
    [scenario] = json.loads(path.read_text())
    path.write_text(json.dumps([scenario, scenario]))
    with pytest.raises(ValueError, match="IDs.*únicos"):
        load_scenarios(path)
    scenario["turns"][0]["approve"] = False
    with pytest.raises(ValidationError, match="exatamente um"):
        EvalScenario.model_validate(scenario)
    path.write_text("[]")
    with pytest.raises(ValidationError):
        load_scenarios(path)


def test_evals_detect_route_mismatch_and_pii_then_exit_one(
    monkeypatch, single_scenario, capsys, isolated_logging
):
    path = single_scenario(expect={"route": ["credito"]})
    monkeypatch.setattr(
        ConversationRuntime,
        "run_turn",
        lambda *args, **kwargs: TurnResult(
            reply="CPF 123.456.789-01",
            conversation_ended=False,
            active_agent="triagem",
            authenticated=False,
        ),
    )
    assert main(["--dataset", str(path), "--json"]) == 1
    report = EvalReport.model_validate_json(capsys.readouterr().out)
    assert report.failed_scenarios == report.pii_leaks == 1
    assert report.route_accuracy == 0
    assert not report.scenarios[0].passed
    assert any("resposta sem PII" in item for item in report.scenarios[0].failures)

    for value in ("1985-11-22", "22/11/1985"):
        assert runner._seed_pii(value, set(), {"1985-11-22"})
    assert not runner._seed_pii("CPF 10000000000, 32/05/1990", set(), {"1990-05-14"})


def test_cli_table_json_and_invalid_dataset(capsys, tmp_path, isolated_logging):
    assert main([]) == 0
    table = capsys.readouterr().out
    assert "Passaram 12/12" in table
    assert "Rotas 100%" in table
    assert "Vazamentos de PII 0" in table
    assert "auth_and_credit_limit_query" in table
    assert main(["--dataset", str(DEFAULT_SCENARIOS_PATH), "--json"]) == 0
    report = EvalReport.model_validate_json(capsys.readouterr().out)
    assert report.failed_scenarios == 0
    missing = tmp_path / "missing.json"
    assert main(["--dataset", str(missing), "--json"]) == 1
    assert "FileNotFoundError" in json.loads(capsys.readouterr().out)["error"]
    assert main(["--dataset", str(missing)]) == 1


def test_unknown_eval_mode_is_rejected_before_the_dataset_is_loaded(tmp_path):
    with pytest.raises(ValueError, match="Modo de avaliação inválido"):
        run_evals(mode="unknown", scenarios_path=tmp_path / "missing.json")


def test_live_evals_use_configured_model_and_flush_observability(
    monkeypatch, scripted_model, single_scenario
):
    path = single_scenario(
        script=["Este roteiro deve ser ignorado."],
        expect={"reply_contains": ["Este roteiro deve ser ignorado."]},
    )
    [scenario] = json.loads(path.read_text())
    scenario["turns"] *= 2
    path.write_text(json.dumps([scenario, scenario | {"id": "second"}]))
    factory = Mock(
        return_value=scripted_model(
            responses=[AIMessage(content="Não posso fazer Pix.") for _ in range(4)]
        )
    )
    monkeypatch.setattr("banco_agil.infrastructure.llm_factory.create_chat_model", factory)
    traces = []

    class Capture(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, *, parent_run_id, metadata, **kwargs):
            if parent_run_id is None:
                traces.append((self, metadata["langfuse_session_id"]))

    sdk = Mock()
    handler_factory = Mock(side_effect=Capture)
    observer = LangfuseObservability(sdk, handler_factory)
    monkeypatch.setattr(runner, "create_langfuse_observability", Mock(return_value=observer))
    report = run_evals(mode="live", scenarios_path=path)
    factory.assert_called_once()
    assert factory.call_args.args[0].llm_model == Settings().llm_model
    assert report.pass_rate == report.route_accuracy == 1.0
    assert handler_factory.call_count == 4
    assert len({handler for handler, _ in traces}) == 4
    first, second, third, fourth = [session for _, session in traces]
    assert first == second
    assert third == fourth
    assert first != third
    sdk.flush.assert_called_once_with()


def test_live_flushes_if_execution_fails(monkeypatch, single_scenario, tmp_path):
    monkeypatch.setattr("banco_agil.infrastructure.llm_factory.create_chat_model", Mock())
    observability = Mock()
    monkeypatch.setattr(runner, "create_langfuse_observability", lambda settings: observability)
    with pytest.raises(FileNotFoundError):
        run_evals(
            mode="live", scenarios_path=single_scenario(), settings=Settings(data_dir=tmp_path)
        )
    observability.flush.assert_called_once_with()

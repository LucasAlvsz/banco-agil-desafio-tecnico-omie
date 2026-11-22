import json
from typing import get_args

import pytest
from pydantic import ValidationError

from banco_agil.agents.state import (
    AGENT_NODES,
    AgentName,
    LimitIncreaseConfirmation,
    LimitIncreaseDecision,
)


def test_agent_nodes_match_agent_names():
    assert get_args(AgentName) == AGENT_NODES


def test_confirmation_round_trips_through_json():
    confirmation = LimitIncreaseConfirmation(limite_atual=2500, novo_limite_solicitado=3000)
    payload = json.loads(json.dumps(confirmation.to_interrupt_payload()))

    assert payload == {
        "action": "confirm_limit_increase",
        "limite_atual": 2500,
        "novo_limite_solicitado": 3000,
        "message": "Você confirma o pedido de aumento de limite?",
    }
    assert LimitIncreaseConfirmation.from_interrupt_value(payload) == confirmation
    assert set(confirmation.model_dump()) == {"limite_atual", "novo_limite_solicitado", "message"}


@pytest.mark.parametrize("value", [None, {}, {"action": "other_action"}])
def test_unrelated_interrupt_is_not_a_confirmation(value):
    assert LimitIncreaseConfirmation.from_interrupt_value(value) is None


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -float("inf")])
def test_confirmation_rejects_nonfinite_amounts(amount):
    with pytest.raises(ValidationError):
        LimitIncreaseConfirmation(limite_atual=2500, novo_limite_solicitado=amount)


@pytest.mark.parametrize("approved", [True, False])
def test_decision_round_trips_boolean(approved):
    payload = LimitIncreaseDecision(approved=approved).to_resume_payload()

    assert payload == {"approved": approved}
    assert LimitIncreaseDecision.is_approved(payload) is approved


@pytest.mark.parametrize(
    "value",
    [
        "sim",
        {},
        {"approved": 1},
        {"approved": "true"},
        {"approved": "sim"},
        {"approved": True, "extra": False},
    ],
)
def test_malformed_decision_does_not_approve(value):
    assert LimitIncreaseDecision.is_approved(value) is False

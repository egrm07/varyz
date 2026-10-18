from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from varyz.personal_model.query import AnswerBasis, QueryDisposition
from varyz.resolution.models import Caller, Resolution, ResolutionRequest
from varyz.resolution.ports import Notifier


def caller() -> Caller:
    return Caller(
        agent_id="agent:codex",
        display_name="Codex",
        purpose="coding",
        device_id="device:local",
    )


@pytest.mark.parametrize("model", [Caller, ResolutionRequest, Resolution])
def test_resolution_contract_models_are_frozen(model: type[object]) -> None:
    assert model.model_config["frozen"] is True  # type: ignore[attr-defined]


def test_nested_resolution_contracts_refuse_mutation() -> None:
    request = ResolutionRequest(
        question="Which API shape did I choose?", caller=caller(), purpose="coding"
    )
    with pytest.raises(ValidationError):
        request.question = "changed"  # type: ignore[misc]


def test_notifier_signature_is_content_free() -> None:
    parameters = tuple(inspect.signature(Notifier.notify).parameters)
    assert parameters == ("self", "escalation_id", "urgency")
    assert not {"question", "answer", "body", "text", "content"}.intersection(parameters)


def test_escalated_resolution_has_no_answer_text() -> None:
    result = Resolution(
        disposition=QueryDisposition.CLARIFICATION,
        basis=AnswerBasis.ESCALATED,
        answer=None,
        tier=5,
        confidence=1,
        escalation_id="esc_1",
        resolved_at=datetime.now(UTC),
        monotonic_seq=1,
    )
    assert result.escalation_id == "esc_1"


def test_terminal_unknown_has_no_basis() -> None:
    result = Resolution(
        disposition=QueryDisposition.NOT_KNOWN,
        basis=None,
        answer=None,
        tier=5,
        confidence=1,
        resolved_at=datetime.now(UTC),
        monotonic_seq=2,
    )
    assert result.basis is None

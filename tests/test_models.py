"""Untrusted JSON must be parsed before reaching SQL."""

import pytest
from pydantic import ValidationError

from dogos_memory import models
from tests.factories import event, thought


@pytest.mark.parametrize("delta", [-1, 101, True, 1.5])
def test_invalid_score_is_rejected_at_boundary(delta: float) -> None:
    # Given
    payload = event().model_dump() | {"affinity_delta": delta}
    # When / Then
    with pytest.raises(ValidationError):
        _ = models.MemoryInput.model_validate(payload)


def test_thought_requires_evidence_at_boundary() -> None:
    # Given
    payload = thought().model_dump() | {"evidence_ids": ()}
    # When / Then
    with pytest.raises(ValidationError):
        _ = models.MemoryInput.model_validate(payload)


def test_llm_cannot_claim_a_completed_fact() -> None:
    # Given
    payload = event().model_dump() | {"source": "llm"}
    # When / Then
    with pytest.raises(ValidationError):
        _ = models.MemoryInput.model_validate(payload)

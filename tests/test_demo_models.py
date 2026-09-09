"""Configuration boundaries that protect identity-derived file paths."""

import pytest
from pydantic import ValidationError

from dogos_demo.models import AdapterId, DogConfig
from dogos_demo.session_models import ActionCommand, ActionName, CommandId
from dogos_memory.models import DogId, SessionId


def test_dog_id_cannot_escape_the_experiment_directory() -> None:
    with pytest.raises(ValidationError):
        _ = DogConfig.model_validate(
            {
                "dog_id": "../outside",
                "name": "unsafe",
                "role": "cautious",
                "adapter_id": "adapter-a",
                "tag_family": "tag36h11",
                "tag_id": 1,
                "familiar_threshold": 60,
                "affinity_gain": 35,
            }
        )


def test_session_id_rejects_slashes_that_make_stable_command_ids_ambiguous() -> None:
    with pytest.raises(ValidationError):
        _ = ActionCommand(
            command_id=CommandId("../old/dog_a/1"),
            session_id=SessionId("../old"),
            step=1,
            actor_id=DogId("dog_a"),
            target_id=DogId("dog_b"),
            adapter_id=AdapterId("adapter-a"),
            action=ActionName.CAUTIOUS_REPLY,
            issued_at_ms=1_000,
        )

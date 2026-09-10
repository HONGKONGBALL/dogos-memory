"""Fixed, reviewable defaults for the software-only hackathon path."""

from __future__ import annotations

from dogos_demo.models import DemoConfig


def default_simulation_config() -> DemoConfig:
    """Return the exact two-dog policy used by the acceptance demo."""
    return DemoConfig.model_validate(
        {
            "mode": "simulation",
            "stable_frames": 3,
            "min_decision_margin": 25.0,
            "max_frame_gap_ms": 200,
            "encounter_cooldown_ms": 1_000,
            "dogs": (
                {
                    "dog_id": "dog_a",
                    "name": "VITA-A",
                    "role": "cautious",
                    "adapter_id": "adapter-a",
                    "tag_family": "tag36h11",
                    "tag_id": 1,
                    "familiar_threshold": 60,
                    "affinity_gain": 35,
                },
                {
                    "dog_id": "dog_b",
                    "name": "VITA-B",
                    "role": "outgoing",
                    "adapter_id": "adapter-b",
                    "tag_family": "tag36h11",
                    "tag_id": 2,
                    "familiar_threshold": 40,
                    "affinity_gain": 25,
                },
            ),
        }
    )

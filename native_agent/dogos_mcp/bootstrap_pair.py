#!/usr/bin/env python3
"""Create or exactly resume the two local DogOS endpoint databases."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    """Initialize two fixed profiles without deleting existing memory."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dogos-source", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--mode", choices=("simulation", "live"), default="simulation")
    args = parser.parse_args()
    sys.path.insert(0, str(args.dogos_source.resolve(strict=True)))
    from dogos_memory.models import DogId, Profile  # noqa: PLC0415
    from dogos_memory.store import MemoryStore  # noqa: PLC0415

    args.directory.mkdir(parents=True, exist_ok=True)
    profiles = (
        Profile(
            dog_id=DogId("dog_a"),
            name="Dog A",
            personality="curious and friendly",
            familiar_threshold=60,
            mode=args.mode,
        ),
        Profile(
            dog_id=DogId("dog_b"),
            name="Dog B",
            personality="careful and warm",
            familiar_threshold=60,
            mode=args.mode,
        ),
    )
    for profile in profiles:
        MemoryStore.initialize(args.directory / f"{profile.dog_id}.db", profile)
    sys.stdout.write(f"{args.directory.resolve()}\n")


if __name__ == "__main__":
    main()

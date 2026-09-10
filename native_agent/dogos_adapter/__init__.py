"""Agent-facing, read-only DogOS integration."""

from .adapter import (
    BoundSocialMemory,
    DogOSAdapterError,
    bind_social_memory,
)

__all__ = [
    "BoundSocialMemory",
    "DogOSAdapterError",
    "bind_social_memory",
]

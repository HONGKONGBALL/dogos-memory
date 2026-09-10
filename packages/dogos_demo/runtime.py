"""Composition root for the bounded two-dog hackathon demo."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, final

from typing_extensions import Self

from dogos_demo.command_execution import CommandExecutor
from dogos_demo.coordinator import SessionCoordinator
from dogos_demo.identity import EncounterGate
from dogos_demo.models import (
    DemoConfig,
    DemoError,
    DogConfig,
    DogRole,
    IdentityDecision,
    ManualEncounter,
    TagFrame,
)
from dogos_demo.pair_memory import PairMemory
from dogos_demo.persistence import CompletionPersistence, StoreBinding
from dogos_demo.policy import dog_with_role
from dogos_memory.models import Profile, SessionId
from dogos_memory.store import MemoryStore

if TYPE_CHECKING:
    from dogos_demo.adapters import ActionAdapter, StopSignal
    from dogos_demo.session_models import PairSnapshot, SessionSummary


@final
class DemoRuntime:
    """Wire identity, policy, adapters, and per-dog memory for one process."""

    def __init__(
        self,
        config: DemoConfig,
        bindings: tuple[StoreBinding, StoreBinding],
        adapters: tuple[ActionAdapter, ActionAdapter],
    ) -> None:
        """Validate fixed adapter bindings before accepting any encounter."""
        expected = {dog.adapter_id for dog in config.dogs}
        actual = {adapter.adapter_id for adapter in adapters}
        if expected != actual:
            raise DemoError("adapter_binding_mismatch", f"expected={expected}, actual={actual}")
        memory = PairMemory(config, bindings)
        persistence = CompletionPersistence(bindings)
        executor = CommandExecutor(config, persistence, adapters)
        cautious = dog_with_role(config, DogRole.CAUTIOUS)
        self._config = config
        self._memory = memory
        self._gate = EncounterGate(config, cautious.dog_id)
        self._coordinator = SessionCoordinator(config, memory, executor)

    @classmethod
    def initialize(
        cls,
        config: DemoConfig,
        directory: Path,
        adapters: tuple[ActionAdapter, ActionAdapter],
    ) -> Self:
        """Create or resume the two fixed per-dog SQLite files."""
        return cls(config, _initialize_bindings(config, directory), adapters)

    @classmethod
    def open(
        cls,
        config: DemoConfig,
        directory: Path,
        adapters: tuple[ActionAdapter, ActionAdapter],
    ) -> Self:
        """Open both existing stores and fail without creating missing paths."""
        runtime = cls(config, _open_bindings(config, directory), adapters)
        _ = runtime.status()
        return runtime

    def run_manual(
        self,
        session_id: SessionId,
        observed_at_ms: int,
        stop: StopSignal | None = None,
    ) -> SessionSummary:
        """Run one pair encounter using the explicit operator identity fallback."""
        cautious = dog_with_role(self._config, DogRole.CAUTIOUS)
        outgoing = dog_with_role(self._config, DogRole.OUTGOING)
        identity = self._gate.confirm_manual(
            ManualEncounter(
                owner_id=cautious.dog_id,
                peer_id=outgoing.dog_id,
                observed_at_ms=observed_at_ms,
            )
        )
        return self.run_identity(session_id, identity, stop)

    def run_identity(
        self,
        session_id: SessionId,
        identity: IdentityDecision,
        stop: StopSignal | None = None,
    ) -> SessionSummary:
        """Run at most two serial commands after identity confirmation."""
        return self._coordinator.run(session_id, identity, stop)

    def observe_tag(self, frame: TagFrame) -> IdentityDecision:
        """Feed one decoded stereo Tag frame into the bounded encounter gate."""
        return self._gate.observe(frame)

    def status(self) -> PairSnapshot:
        """Read a fresh two-dog dashboard snapshot from SQLite."""
        return self._memory.snapshot()


def _initialize_bindings(
    config: DemoConfig,
    directory: Path,
) -> tuple[StoreBinding, StoreBinding]:
    bindings = tuple(
        StoreBinding(
            dog=dog,
            store=MemoryStore.initialize(directory / f"{dog.dog_id}.db", _profile(config, dog)),
        )
        for dog in config.dogs
    )
    return bindings[0], bindings[1]


def _open_bindings(
    config: DemoConfig,
    directory: Path,
) -> tuple[StoreBinding, StoreBinding]:
    bindings = tuple(
        StoreBinding(
            dog=dog,
            store=MemoryStore(directory / f"{dog.dog_id}.db", dog.dog_id),
        )
        for dog in config.dogs
    )
    return bindings[0], bindings[1]


def _profile(config: DemoConfig, dog: DogConfig) -> Profile:
    return Profile(
        dog_id=dog.dog_id,
        name=dog.name,
        personality=dog.role.value,
        familiar_threshold=dog.familiar_threshold,
        mode=config.mode,
    )

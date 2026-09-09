"""Idempotent completion projection into two independent memory databases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias, final

from typing_extensions import assert_never

from dogos_demo.models import DemoError, DogConfig
from dogos_demo.session_models import (
    ActionCommand,
    ActionReceipt,
    CompletionSource,
    PersistenceIssue,
    ReceiptStatus,
)
from dogos_memory.models import DogId, MemoryId, MemoryInput, StoredMemory, StoreError

if TYPE_CHECKING:
    from dogos_memory.store import MemoryStore

MemorySource: TypeAlias = Literal["robot_feedback", "operator", "simulation"]


@dataclass(frozen=True, slots=True)
class StoreBinding:
    """One verified dog-to-database ownership binding."""

    dog: DogConfig
    store: MemoryStore


@dataclass(frozen=True, slots=True)
class PersistOutcome:
    """Per-database result of projecting one completed command."""

    persisted_owner_ids: tuple[DogId, ...]
    issues: tuple[PersistenceIssue, ...]


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Existing completion receipt plus any attempted backfill result."""

    receipt: ActionReceipt | None
    persistence: PersistOutcome | None


@final
class CompletionPersistence:
    """Project one verified completion to both stores using stable IDs."""

    def __init__(
        self,
        bindings: tuple[StoreBinding, StoreBinding],
    ) -> None:
        """Bind the two owner-verified stores in deterministic order."""
        self._bindings = bindings

    def recover(self, command: ActionCommand) -> RecoveryOutcome:
        """Backfill a partial prior write without re-executing its action."""
        memory_id = _completion_memory_id(command)
        existing = tuple(self._find(binding.store, memory_id) for binding in self._bindings)
        present = tuple(memory for memory in existing if memory is not None)
        if not present:
            return RecoveryOutcome(receipt=None, persistence=None)
        anchor = present[0]
        self._check_existing(command, anchor)
        if any(
            memory is not None
            and (memory.source != anchor.source or memory.occurred_at_ms != anchor.occurred_at_ms)
            for memory in existing
        ):
            raise DemoError("completion_conflict", str(command.command_id))
        receipt = ActionReceipt(
            status=ReceiptStatus.COMPLETED,
            command_id=command.command_id,
            adapter_id=command.adapter_id,
            occurred_at_ms=anchor.occurred_at_ms,
            source=_completion_source(anchor.source),
            detail="recovered from stable completed-memory ID",
        )
        return RecoveryOutcome(receipt=receipt, persistence=self.persist(command, receipt))

    def persist(self, command: ActionCommand, receipt: ActionReceipt) -> PersistOutcome:
        """Attempt both stores; a successful side remains safe to replay."""
        if receipt.status != ReceiptStatus.COMPLETED or receipt.source is None:
            raise DemoError("unconfirmed_persistence", str(command.command_id))
        persisted: list[DogId] = []
        issues: list[PersistenceIssue] = []
        for binding in self._bindings:
            memory = self._memory(binding.dog, command, receipt)
            try:
                _ = binding.store.append(memory)
                persisted.append(binding.dog.dog_id)
            except StoreError as error:
                issues.append(PersistenceIssue(owner_id=binding.dog.dog_id, code=error.code))
        return PersistOutcome(tuple(persisted), tuple(issues))

    def _memory(
        self,
        owner: DogConfig,
        command: ActionCommand,
        receipt: ActionReceipt,
    ) -> MemoryInput:
        source = receipt.source
        if source is None:
            raise DemoError("missing_completion_source", str(command.command_id))
        is_target = owner.dog_id == command.target_id
        peer_id = command.actor_id if is_target else command.target_id
        direction = "对方向我" if is_target else "我向对方"
        return MemoryInput(
            memory_id=_completion_memory_id(command),
            session_id=command.session_id,
            peer_id=peer_id,
            kind="event",
            event_type="action_completed",
            content=f"{direction}完成动作 {command.action.value}",
            occurred_at_ms=receipt.occurred_at_ms,
            source=_memory_source(source),
            result="completed",
            affinity_delta=owner.affinity_gain if is_target else 0,
        )

    def _check_existing(self, command: ActionCommand, memory: StoredMemory) -> None:
        expected_peers = {command.actor_id, command.target_id}
        valid = (
            memory.memory_id == _completion_memory_id(command)
            and memory.session_id == command.session_id
            and memory.peer_id in expected_peers
            and memory.event_type == "action_completed"
            and memory.result == "completed"
        )
        if not valid:
            raise DemoError("invalid_recovered_completion", str(command.command_id))

    @staticmethod
    def _find(store: MemoryStore, memory_id: MemoryId) -> StoredMemory | None:
        try:
            return store.get(memory_id)
        except StoreError as error:
            if _is_missing(error):
                return None
            raise


def _is_missing(error: StoreError) -> bool:
    return error.code == "memory_not_found"


def _completion_memory_id(command: ActionCommand) -> MemoryId:
    return MemoryId(f"{command.command_id}/completed")


def _memory_source(source: CompletionSource) -> MemorySource:
    match source:
        case CompletionSource.ROBOT_FEEDBACK:
            return "robot_feedback"
        case CompletionSource.OPERATOR:
            return "operator"
        case CompletionSource.SIMULATION:
            return "simulation"
        case unreachable:
            assert_never(unreachable)


def _completion_source(source: str) -> CompletionSource:
    try:
        return CompletionSource(source)
    except ValueError as error:
        raise DemoError("invalid_completion_source", source) from error

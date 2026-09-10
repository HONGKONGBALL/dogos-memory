"""Validated command, completion, and session result models."""

from __future__ import annotations

from enum import unique
from typing import Annotated, Final, Literal, NewType

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError
from typing_extensions import Self, assert_never

from dogos_demo.models import AdapterId, BoundaryModel, IdentityDecision, StringEnum
from dogos_memory.models import DogId, MemoryId, SessionId, Timestamp

CommandId = NewType("CommandId", str)
MAX_SESSION_STEPS: Final = 2
SessionIdentifier = StringConstraints(
    strip_whitespace=True,
    min_length=1,
    max_length=100,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
)


@unique
class ActionName(StringEnum):
    """Small software whitelist; hardware mapping remains adapter-owned."""

    OUTGOING_GREETING = "outgoing_greeting"
    CAUTIOUS_REPLY = "cautious_reply"
    FAMILIAR_GREETING = "familiar_greeting"
    WARM_REPLY = "warm_reply"


@unique
class ReceiptStatus(StringEnum):
    """Closed adapter outcomes consumed by the completion gate."""

    COMPLETED = "completed"
    SERVICE_ACKNOWLEDGED = "service_acknowledged"
    FAILED = "failed"
    CANCELLED = "cancelled"


@unique
class CompletionSource(StringEnum):
    """Evidence allowed to create a completed memory."""

    ROBOT_FEEDBACK = "robot_feedback"
    OPERATOR = "operator"
    SIMULATION = "simulation"


@unique
class SessionStatus(StringEnum):
    """Terminal session outcomes."""

    COMPLETED = "completed"
    ABORTED = "aborted"


@unique
class AbortReason(StringEnum):
    """Reasons that stop all remaining commands in a session."""

    IDENTITY_NOT_CONFIRMED = "identity_not_confirmed"
    STOP_REQUESTED = "stop_requested"
    SERVICE_UNCONFIRMED = "service_unconfirmed"
    ACTION_FAILED = "action_failed"
    ACTION_CANCELLED = "action_cancelled"
    ROUTING_CONFLICT = "routing_conflict"
    COMPLETION_SOURCE_MISMATCH = "completion_source_mismatch"
    PERSISTENCE_PENDING = "persistence_pending"


class ActionCommand(BoundaryModel):
    """One deterministic, pair-scoped action dispatch."""

    command_id: Annotated[CommandId, Field(min_length=1, max_length=400)]
    session_id: Annotated[SessionId, SessionIdentifier]
    step: Annotated[int, Field(strict=True, ge=1, le=2)]
    actor_id: DogId
    target_id: DogId
    adapter_id: AdapterId
    action: ActionName
    issued_at_ms: Timestamp
    memory_reference_id: MemoryId | None = None

    @model_validator(mode="after")
    def check_command_contract(self) -> Self:
        """Prevent self-actions and unstable command identifiers."""
        expected = f"{self.session_id}/{self.actor_id}/{self.step}"
        if self.actor_id == self.target_id or self.command_id != expected:
            raise PydanticCustomError("command_contract", "Invalid actor, target, or command ID")
        return self


class ActionReceipt(BoundaryModel):
    """Adapter result; only completed receipts carry completion evidence."""

    status: ReceiptStatus
    command_id: CommandId
    adapter_id: AdapterId
    occurred_at_ms: Timestamp
    source: CompletionSource | None = None
    detail: Annotated[str, Field(min_length=1, max_length=1_000)]

    @model_validator(mode="after")
    def check_receipt_contract(self) -> Self:
        """Make service acknowledgement impossible to label as completion."""
        match self.status:
            case ReceiptStatus.COMPLETED:
                valid = self.source is not None
            case (
                ReceiptStatus.SERVICE_ACKNOWLEDGED | ReceiptStatus.FAILED | ReceiptStatus.CANCELLED
            ):
                valid = self.source is None
            case unreachable:
                assert_never(unreachable)
        if not valid:
            raise PydanticCustomError("receipt_contract", "Completion source does not match status")
        return self


class DogMemoryView(BoundaryModel):
    """Small dashboard view derived from a real recall query."""

    dog_id: DogId
    peer_id: DogId
    affinity: Annotated[int, Field(strict=True, ge=0, le=100)]
    familiar: bool
    latest_memory_id: MemoryId | None = None


class PairSnapshot(BoundaryModel):
    """Role-addressed two-dog memory state."""

    mode: Literal["live", "simulation"]
    cautious: DogMemoryView
    outgoing: DogMemoryView


class PersistenceIssue(BoundaryModel):
    """One per-dog database that did not accept a stable completion ID."""

    owner_id: DogId
    code: Annotated[str, Field(min_length=1, max_length=200)]


class CommandReport(BoundaryModel):
    """Action evidence plus per-owner persistence status."""

    command: ActionCommand
    receipt: ActionReceipt
    replayed: bool
    persisted_owner_ids: tuple[DogId, ...] = ()
    persistence_issues: tuple[PersistenceIssue, ...] = ()


class SessionSummary(BoundaryModel):
    """One terminal encounter result suitable for CLI/UI serialization."""

    session_id: Annotated[SessionId, SessionIdentifier]
    status: SessionStatus
    abort_reason: AbortReason | None
    identity: IdentityDecision
    initiator_id: DogId | None
    before: PairSnapshot
    after: PairSnapshot
    commands: tuple[CommandReport, ...]

    @model_validator(mode="after")
    def check_session_contract(self) -> Self:
        """A completed session is exactly two confirmed, persisted commands."""
        match self.status:
            case SessionStatus.COMPLETED:
                valid = (
                    self.abort_reason is None
                    and self.initiator_id is not None
                    and len(self.commands) == MAX_SESSION_STEPS
                    and all(
                        report.receipt.status == ReceiptStatus.COMPLETED
                        and not report.persistence_issues
                        for report in self.commands
                    )
                )
            case SessionStatus.ABORTED:
                valid = self.abort_reason is not None and len(self.commands) <= MAX_SESSION_STEPS
            case unreachable:
                assert_never(unreachable)
        if not valid:
            raise PydanticCustomError("session_contract", "Invalid terminal session state")
        return self

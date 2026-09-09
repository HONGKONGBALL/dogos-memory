"""Deterministic two-step policy driven only by recalled relationship state."""

from __future__ import annotations

from dataclasses import dataclass

from dogos_demo.models import DemoConfig, DemoError, DogConfig, DogRole, IdentityDecision
from dogos_demo.session_models import ActionCommand, ActionName, CommandId
from dogos_memory.models import DogId, MemoryId, RecallResult, SessionId


@dataclass(frozen=True, slots=True)
class PairRecalls:
    """Recall snapshots addressed by policy role rather than tuple position."""

    cautious: RecallResult
    outgoing: RecallResult


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Fields that vary between the two deterministic policy steps."""

    step: int
    actor: DogConfig
    target: DogConfig
    action: ActionName
    issued_at_ms: int
    memory_reference_id: MemoryId | None


def dog_with_role(config: DemoConfig, role: DogRole) -> DogConfig:
    """Resolve the single role guaranteed by DemoConfig."""
    matches = tuple(dog for dog in config.dogs if dog.role == role)
    if len(matches) != 1:
        raise DemoError("role_configuration", role.value)
    return matches[0]


def build_plan(
    config: DemoConfig,
    identity: IdentityDecision,
    session_id: SessionId,
    recalls: PairRecalls,
) -> tuple[ActionCommand, ActionCommand]:
    """Choose two bounded actions; familiarity changes who initiates."""
    cautious = dog_with_role(config, DogRole.CAUTIOUS)
    outgoing = dog_with_role(config, DogRole.OUTGOING)
    initiator_id = cautious.dog_id if recalls.cautious.familiar else outgoing.dog_id
    return build_plan_for_initiator(
        config,
        identity,
        session_id,
        recalls,
        initiator_id,
    )


def build_plan_for_initiator(
    config: DemoConfig,
    identity: IdentityDecision,
    session_id: SessionId,
    recalls: PairRecalls,
    initiator_id: DogId,
) -> tuple[ActionCommand, ActionCommand]:
    """Rebuild a persisted session plan from its original step-one actor."""
    cautious = dog_with_role(config, DogRole.CAUTIOUS)
    outgoing = dog_with_role(config, DogRole.OUTGOING)
    if initiator_id == cautious.dog_id:
        first = _command(
            session_id,
            CommandSpec(
                1,
                cautious,
                outgoing,
                ActionName.FAMILIAR_GREETING,
                identity.observed_at_ms,
                _latest_fact(recalls.cautious),
            ),
        )
        second = _command(
            session_id,
            CommandSpec(
                2,
                outgoing,
                cautious,
                ActionName.WARM_REPLY,
                identity.observed_at_ms + 1,
                _latest_fact(recalls.outgoing),
            ),
        )
        return first, second
    if initiator_id != outgoing.dog_id:
        raise DemoError("initiator_not_configured", str(initiator_id))
    first = _command(
        session_id,
        CommandSpec(
            1,
            outgoing,
            cautious,
            ActionName.OUTGOING_GREETING,
            identity.observed_at_ms,
            _latest_fact(recalls.outgoing),
        ),
    )
    second = _command(
        session_id,
        CommandSpec(
            2,
            cautious,
            outgoing,
            ActionName.CAUTIOUS_REPLY,
            identity.observed_at_ms + 1,
            _latest_fact(recalls.cautious),
        ),
    )
    return first, second


def _latest_fact(recall: RecallResult) -> MemoryId | None:
    return recall.facts[0].memory_id if recall.facts else None


def _command(
    session_id: SessionId,
    spec: CommandSpec,
) -> ActionCommand:
    command_id = CommandId(f"{session_id}/{spec.actor.dog_id}/{spec.step}")
    return ActionCommand(
        command_id=command_id,
        session_id=session_id,
        step=spec.step,
        actor_id=DogId(spec.actor.dog_id),
        target_id=DogId(spec.target.dog_id),
        adapter_id=spec.actor.adapter_id,
        action=spec.action,
        issued_at_ms=spec.issued_at_ms,
        memory_reference_id=spec.memory_reference_id,
    )

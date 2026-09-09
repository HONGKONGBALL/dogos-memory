"""Two-dog policy, completion, persistence, and recovery integration tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import final

from dogos_demo.adapters import (
    ActionAdapter,
    ServiceAcknowledgementAdapter,
    SimulationAdapter,
    StopToken,
)
from dogos_demo.models import AdapterId, IdentityStatus, TagDetection, TagFamily, TagFrame
from dogos_demo.runtime import DemoRuntime
from dogos_demo.session_models import (
    AbortReason,
    ActionCommand,
    ActionName,
    ActionReceipt,
    CompletionSource,
    ReceiptStatus,
    SessionStatus,
    SessionSummary,
)
from dogos_memory.models import DogId, SessionId
from tests.demo_factories import demo_config


def simulation_pair() -> tuple[SimulationAdapter, SimulationAdapter]:
    return (
        SimulationAdapter(AdapterId("adapter-a")),
        SimulationAdapter(AdapterId("adapter-b")),
    )


def run_session(
    runtime: DemoRuntime,
    session_id: str,
    observed_at_ms: int,
) -> SessionSummary:
    return runtime.run_manual(SessionId(session_id), observed_at_ms)


def test_two_step_session_uses_fixed_routes_and_scores_only_completed_peer_actions(
    tmp_path: Path,
) -> None:
    adapters = simulation_pair()
    runtime = DemoRuntime.initialize(demo_config(), tmp_path, adapters)

    result = run_session(runtime, "encounter-1", 1_000)

    assert result.status == SessionStatus.COMPLETED
    assert result.initiator_id == DogId("dog_b")
    assert tuple(report.command.action for report in result.commands) == (
        ActionName.OUTGOING_GREETING,
        ActionName.CAUTIOUS_REPLY,
    )
    assert tuple(report.command.adapter_id for report in result.commands) == (
        AdapterId("adapter-b"),
        AdapterId("adapter-a"),
    )
    assert (adapters[0].execution_count, adapters[1].execution_count) == (1, 1)
    assert (result.after.cautious.affinity, result.after.outgoing.affinity) == (35, 25)
    assert all(
        report.persisted_owner_ids == (DogId("dog_a"), DogId("dog_b")) for report in result.commands
    )


def test_runtime_feeds_confirmed_apriltag_into_the_same_session_path(tmp_path: Path) -> None:
    runtime = DemoRuntime.initialize(demo_config(), tmp_path, simulation_pair())
    detection = TagDetection(
        family=TagFamily("tag36h11"),
        tag_id=2,
        decision_margin=50.0,
    )
    decisions = tuple(
        runtime.observe_tag(
            TagFrame(
                owner_id=DogId("dog_a"),
                observed_at_ms=observed_at_ms,
                left=detection,
                right=detection,
            )
        )
        for observed_at_ms in (1_000, 1_100, 1_200)
    )

    result = runtime.run_identity(SessionId("encounter-tag"), decisions[-1])

    assert decisions[-1].status == IdentityStatus.CONFIRMED
    assert result.status == SessionStatus.COMPLETED
    assert result.identity.source is not None


def test_restart_uses_persisted_familiarity_to_change_the_initiator(tmp_path: Path) -> None:
    first_runtime = DemoRuntime.initialize(demo_config(), tmp_path, simulation_pair())
    _ = run_session(first_runtime, "encounter-1", 1_000)
    second = run_session(first_runtime, "encounter-2", 3_000)
    assert (second.after.cautious.affinity, second.after.outgoing.affinity) == (70, 50)

    restarted = DemoRuntime.initialize(demo_config(), tmp_path, simulation_pair())
    reunion = run_session(restarted, "encounter-3", 5_000)

    assert reunion.before.cautious.familiar
    assert reunion.before.outgoing.familiar
    assert reunion.initiator_id == DogId("dog_a")
    assert reunion.commands[0].command.action == ActionName.FAMILIAR_GREETING
    assert reunion.commands[0].command.memory_reference_id is not None


def test_replaying_same_session_skips_adapters_and_does_not_inflate_scores(
    tmp_path: Path,
) -> None:
    original = DemoRuntime.initialize(demo_config(), tmp_path, simulation_pair())
    completed = run_session(original, "encounter-replay", 1_000)
    adapters = simulation_pair()
    restarted = DemoRuntime.initialize(demo_config(), tmp_path, adapters)

    replayed = run_session(restarted, "encounter-replay", 2_000)

    assert replayed.status == SessionStatus.COMPLETED
    assert all(report.replayed for report in replayed.commands)
    assert (adapters[0].execution_count, adapters[1].execution_count) == (0, 0)
    assert replayed.after == completed.after


def test_replaying_older_session_after_policy_change_recovers_original_plan(
    tmp_path: Path,
) -> None:
    original = DemoRuntime.initialize(demo_config(), tmp_path, simulation_pair())
    first = run_session(original, "encounter-old", 1_000)
    _ = run_session(original, "encounter-new", 3_000)
    adapters = simulation_pair()
    restarted = DemoRuntime.initialize(demo_config(), tmp_path, adapters)

    replayed = run_session(restarted, "encounter-old", 5_000)

    assert replayed.commands[0].command.action == first.commands[0].command.action
    assert replayed.initiator_id == first.initiator_id
    assert all(report.replayed for report in replayed.commands)
    assert (adapters[0].execution_count, adapters[1].execution_count) == (0, 0)
    assert (replayed.after.cautious.affinity, replayed.after.outgoing.affinity) == (70, 50)


def test_service_acknowledgement_is_unconfirmed_and_never_changes_memory(
    tmp_path: Path,
) -> None:
    adapters: tuple[ActionAdapter, ActionAdapter] = (
        SimulationAdapter(AdapterId("adapter-a")),
        ServiceAcknowledgementAdapter(AdapterId("adapter-b")),
    )
    runtime = DemoRuntime.initialize(demo_config(), tmp_path, adapters)

    result = run_session(runtime, "encounter-unconfirmed", 1_000)

    assert result.status == SessionStatus.ABORTED
    assert result.abort_reason == AbortReason.SERVICE_UNCONFIRMED
    assert len(result.commands) == 1
    assert result.commands[0].receipt.status == ReceiptStatus.SERVICE_ACKNOWLEDGED
    assert (result.after.cautious.affinity, result.after.outgoing.affinity) == (0, 0)


@final
class WrongReceiptAdapter:
    """External boundary double that returns a receipt for the wrong route."""

    def __init__(self, adapter_id: AdapterId) -> None:
        self._adapter_id = adapter_id

    @property
    def adapter_id(self) -> AdapterId:
        return self._adapter_id

    def execute(self, command: ActionCommand) -> ActionReceipt:
        return ActionReceipt(
            status=ReceiptStatus.COMPLETED,
            command_id=command.command_id,
            adapter_id=AdapterId("another-adapter"),
            occurred_at_ms=command.issued_at_ms,
            source=CompletionSource.SIMULATION,
            detail="wrong adapter",
        )


def test_receipt_for_wrong_adapter_aborts_before_any_memory_write(tmp_path: Path) -> None:
    adapters: tuple[ActionAdapter, ActionAdapter] = (
        SimulationAdapter(AdapterId("adapter-a")),
        WrongReceiptAdapter(AdapterId("adapter-b")),
    )
    runtime = DemoRuntime.initialize(demo_config(), tmp_path, adapters)

    result = run_session(runtime, "encounter-misroute", 1_000)

    assert result.status == SessionStatus.ABORTED
    assert result.abort_reason == AbortReason.ROUTING_CONFLICT
    assert (result.after.cautious.affinity, result.after.outgoing.affinity) == (0, 0)


def test_stop_requested_before_first_step_dispatches_nothing(tmp_path: Path) -> None:
    adapters = simulation_pair()
    runtime = DemoRuntime.initialize(demo_config(), tmp_path, adapters)
    stop = StopToken()
    stop.request_stop()

    result = runtime.run_manual(SessionId("encounter-stop"), 1_000, stop)

    assert result.status == SessionStatus.ABORTED
    assert result.abort_reason == AbortReason.STOP_REQUESTED
    assert result.commands == ()
    assert (adapters[0].execution_count, adapters[1].execution_count) == (0, 0)


def test_partial_two_database_write_is_backfilled_without_reexecuting_action(
    tmp_path: Path,
) -> None:
    first_adapters = simulation_pair()
    first = DemoRuntime.initialize(demo_config(), tmp_path, first_adapters)
    with sqlite3.connect(tmp_path / "dog_b.db") as connection:
        _ = connection.execute(
            """CREATE TRIGGER injected_failure BEFORE INSERT ON memories
            WHEN NEW.event_type = 'action_completed'
            BEGIN SELECT RAISE(ABORT, 'injected_failure'); END"""
        )

    interrupted = run_session(first, "encounter-partial", 1_000)

    assert interrupted.status == SessionStatus.ABORTED
    assert interrupted.abort_reason == AbortReason.PERSISTENCE_PENDING
    assert interrupted.commands[0].persisted_owner_ids == (DogId("dog_a"),)
    with sqlite3.connect(tmp_path / "dog_b.db") as connection:
        _ = connection.execute("DROP TRIGGER injected_failure")

    retry_adapters = simulation_pair()
    retried = DemoRuntime.initialize(demo_config(), tmp_path, retry_adapters)
    recovered = run_session(retried, "encounter-partial", 2_000)

    assert recovered.status == SessionStatus.COMPLETED
    assert recovered.commands[0].replayed
    assert (retry_adapters[0].execution_count, retry_adapters[1].execution_count) == (1, 0)
    assert (recovered.after.cautious.affinity, recovered.after.outgoing.affinity) == (35, 25)

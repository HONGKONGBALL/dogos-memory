"""Bounded two-step session orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from typing_extensions import assert_never

from dogos_demo.adapters import NeverStop
from dogos_demo.command_execution import CommandAccepted, CommandExecutor, CommandRejected
from dogos_demo.models import DemoConfig, IdentityDecision, IdentityStatus
from dogos_demo.policy import build_plan, build_plan_for_initiator
from dogos_demo.session_models import (
    AbortReason,
    CommandReport,
    PairSnapshot,
    SessionStatus,
    SessionSummary,
)

if TYPE_CHECKING:
    from dogos_demo.adapters import StopSignal
    from dogos_demo.pair_memory import PairMemory
    from dogos_memory.models import DogId, SessionId


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Stable values shared by all terminal summary paths."""

    session_id: SessionId
    identity: IdentityDecision
    initiator_id: DogId | None
    before: PairSnapshot


@final
class SessionCoordinator:
    """Plan once, execute serially, and stop after the first unsafe outcome."""

    def __init__(
        self,
        config: DemoConfig,
        memory: PairMemory,
        executor: CommandExecutor,
    ) -> None:
        """Bind deterministic policy, memory reads, and command execution."""
        self._config = config
        self._memory = memory
        self._executor = executor

    def run(
        self,
        session_id: SessionId,
        identity: IdentityDecision,
        stop: StopSignal | None = None,
    ) -> SessionSummary:
        """Run zero or two actions and return one validated terminal summary."""
        recalls = self._memory.recalls()
        before = self._memory.snapshot()
        match identity.status:
            case IdentityStatus.CONFIRMED:
                original = self._memory.original_initiator(session_id)
                plan = (
                    build_plan(self._config, identity, session_id, recalls)
                    if original is None
                    else build_plan_for_initiator(
                        self._config,
                        identity,
                        session_id,
                        recalls,
                        original,
                    )
                )
            case IdentityStatus.PENDING | IdentityStatus.REJECTED | IdentityStatus.SUPPRESSED:
                context = SessionContext(session_id, identity, None, before)
                return self._aborted(context, (), AbortReason.IDENTITY_NOT_CONFIRMED)
            case unreachable:
                assert_never(unreachable)

        context = SessionContext(session_id, identity, plan[0].actor_id, before)
        reports: list[CommandReport] = []
        stop_signal = NeverStop() if stop is None else stop
        for command in plan:
            if stop_signal.should_stop():
                return self._aborted(context, tuple(reports), AbortReason.STOP_REQUESTED)
            match self._executor.execute(command):
                case CommandAccepted(report=report):
                    reports.append(report)
                case CommandRejected(report=report, reason=reason):
                    reports.append(report)
                    return self._aborted(context, tuple(reports), reason)
                case unreachable:
                    assert_never(unreachable)
        return SessionSummary(
            session_id=session_id,
            status=SessionStatus.COMPLETED,
            abort_reason=None,
            identity=identity,
            initiator_id=context.initiator_id,
            before=before,
            after=self._memory.snapshot(),
            commands=tuple(reports),
        )

    def _aborted(
        self,
        context: SessionContext,
        commands: tuple[CommandReport, ...],
        reason: AbortReason,
    ) -> SessionSummary:
        return SessionSummary(
            session_id=context.session_id,
            status=SessionStatus.ABORTED,
            abort_reason=reason,
            identity=context.identity,
            initiator_id=context.initiator_id,
            before=context.before,
            after=self._memory.snapshot(),
            commands=commands,
        )

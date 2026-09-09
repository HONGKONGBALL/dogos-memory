"""One-command routing, completion gating, and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, final

from typing_extensions import assert_never

from dogos_demo.models import DemoConfig, DemoError
from dogos_demo.persistence import CompletionPersistence, PersistOutcome
from dogos_demo.session_models import (
    AbortReason,
    ActionCommand,
    ActionReceipt,
    CommandReport,
    CompletionSource,
    ReceiptStatus,
)

if TYPE_CHECKING:
    from dogos_demo.adapters import ActionAdapter


@dataclass(frozen=True, slots=True)
class CommandAccepted:
    """A completion was persisted to both per-dog stores."""

    report: CommandReport


@dataclass(frozen=True, slots=True)
class CommandRejected:
    """A terminal command result that stops the remaining session."""

    report: CommandReport
    reason: AbortReason


CommandOutcome: TypeAlias = CommandAccepted | CommandRejected


@final
class CommandExecutor:
    """Execute one command through its fixed adapter and completion gate."""

    def __init__(
        self,
        config: DemoConfig,
        persistence: CompletionPersistence,
        adapters: tuple[ActionAdapter, ActionAdapter],
    ) -> None:
        """Bind mode policy, persistence, and two verified adapter routes."""
        self._config = config
        self._persistence = persistence
        self._adapters = adapters

    def execute(self, command: ActionCommand) -> CommandOutcome:
        """Recover or dispatch one command without double execution."""
        recovered = self._persistence.recover(command)
        if recovered.receipt is not None and recovered.persistence is not None:
            report = _report(command, recovered.receipt, recovered.persistence, replayed=True)
            if recovered.persistence.issues:
                return CommandRejected(report, AbortReason.PERSISTENCE_PENDING)
            return CommandAccepted(report)

        receipt = self._adapter(command).execute(command)
        if not _route_matches(command, receipt):
            return CommandRejected(
                _report(command, receipt, _empty_persist(), replayed=False),
                AbortReason.ROUTING_CONFLICT,
            )
        match receipt.status:
            case ReceiptStatus.COMPLETED:
                return self._completed(command, receipt)
            case ReceiptStatus.SERVICE_ACKNOWLEDGED:
                reason = AbortReason.SERVICE_UNCONFIRMED
            case ReceiptStatus.FAILED:
                reason = AbortReason.ACTION_FAILED
            case ReceiptStatus.CANCELLED:
                reason = AbortReason.ACTION_CANCELLED
            case unreachable:
                assert_never(unreachable)
        return CommandRejected(
            _report(command, receipt, _empty_persist(), replayed=False),
            reason,
        )

    def _completed(self, command: ActionCommand, receipt: ActionReceipt) -> CommandOutcome:
        source = receipt.source
        if source is None or not self._source_allowed(source):
            return CommandRejected(
                _report(command, receipt, _empty_persist(), replayed=False),
                AbortReason.COMPLETION_SOURCE_MISMATCH,
            )
        persisted = self._persistence.persist(command, receipt)
        report = _report(command, receipt, persisted, replayed=False)
        if persisted.issues:
            return CommandRejected(report, AbortReason.PERSISTENCE_PENDING)
        return CommandAccepted(report)

    def _adapter(self, command: ActionCommand) -> ActionAdapter:
        adapter = next(
            (item for item in self._adapters if item.adapter_id == command.adapter_id),
            None,
        )
        if adapter is None:
            raise DemoError("adapter_binding_missing", str(command.adapter_id))
        return adapter

    def _source_allowed(self, source: CompletionSource) -> bool:
        match self._config.mode:
            case "simulation":
                return source == CompletionSource.SIMULATION
            case "live":
                return source in {CompletionSource.ROBOT_FEEDBACK, CompletionSource.OPERATOR}
            case unreachable:
                assert_never(unreachable)


def _route_matches(command: ActionCommand, receipt: ActionReceipt) -> bool:
    return receipt.command_id == command.command_id and receipt.adapter_id == command.adapter_id


def _empty_persist() -> PersistOutcome:
    return PersistOutcome((), ())


def _report(
    command: ActionCommand,
    receipt: ActionReceipt,
    persistence: PersistOutcome,
    *,
    replayed: bool,
) -> CommandReport:
    return CommandReport(
        command=command,
        receipt=receipt,
        replayed=replayed,
        persisted_owner_ids=persistence.persisted_owner_ids,
        persistence_issues=persistence.issues,
    )

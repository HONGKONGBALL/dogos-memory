"""Small adapter boundary and safe non-hardware implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, final

from dogos_demo.session_models import (
    ActionCommand,
    ActionReceipt,
    CommandId,
    CompletionSource,
    ReceiptStatus,
)

if TYPE_CHECKING:
    from dogos_demo.models import AdapterId


class ActionAdapter(Protocol):
    """A fixed route capable of executing one validated whitelist command."""

    @property
    def adapter_id(self) -> AdapterId:
        """Return the route identity bound to this adapter."""
        ...

    def execute(self, command: ActionCommand) -> ActionReceipt:
        """Execute one validated command and return typed evidence."""
        ...


class StopSignal(Protocol):
    """Cooperative stop checked before every adapter dispatch."""

    def should_stop(self) -> bool:
        """Return whether dispatch must stop before the next command."""
        ...


@final
class StopToken:
    """Mutable operator stop token shared with one synchronous session."""

    def __init__(self) -> None:
        """Create a token that initially permits dispatch."""
        self._requested = False

    def request_stop(self) -> None:
        """Prevent the next command from being dispatched."""
        self._requested = True

    def should_stop(self) -> bool:
        """Return the current cooperative-stop state."""
        return self._requested


@final
class NeverStop:
    """Default stop signal for a non-interrupted synchronous call."""

    def should_stop(self) -> bool:
        """Always permit the next dispatch."""
        return False


@final
class SimulationAdapter:
    """Execute only in software and retain a local dispatch trace."""

    def __init__(self, adapter_id: AdapterId) -> None:
        """Bind a software-only executor to one adapter identity."""
        self._adapter_id = adapter_id
        self._executed: list[CommandId] = []

    @property
    def adapter_id(self) -> AdapterId:
        """Return this simulation route's immutable identity."""
        return self._adapter_id

    @property
    def execution_count(self) -> int:
        """Expose actual software dispatches for the demo console and tests."""
        return len(self._executed)

    def execute(self, command: ActionCommand) -> ActionReceipt:
        """Return simulation evidence without claiming a physical effect."""
        self._executed.append(command.command_id)
        return ActionReceipt(
            status=ReceiptStatus.COMPLETED,
            command_id=command.command_id,
            adapter_id=self._adapter_id,
            occurred_at_ms=command.issued_at_ms,
            source=CompletionSource.SIMULATION,
            detail="software simulation completed; physical effect not claimed",
        )


@final
class ServiceAcknowledgementAdapter:
    """Represent a live service ACK whose physical effect is still unknown."""

    def __init__(self, adapter_id: AdapterId) -> None:
        """Bind an acknowledgement-only executor to one route."""
        self._adapter_id = adapter_id

    @property
    def adapter_id(self) -> AdapterId:
        """Return this acknowledgement route's immutable identity."""
        return self._adapter_id

    def execute(self, command: ActionCommand) -> ActionReceipt:
        """Return unconfirmed evidence that the completion gate must reject."""
        return ActionReceipt(
            status=ReceiptStatus.SERVICE_ACKNOWLEDGED,
            command_id=command.command_id,
            adapter_id=self._adapter_id,
            occurred_at_ms=command.issued_at_ms,
            detail="service acknowledged request; physical completion unverified",
        )

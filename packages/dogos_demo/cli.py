"""CLI for the restartable, software-only two-dog encounter demo."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Final

import typer
from pydantic import ValidationError
from typing_extensions import assert_never

from dogos_demo.adapters import SimulationAdapter
from dogos_demo.config import default_simulation_config
from dogos_demo.connection_models import PairConnectionConfig, PairConnectionState
from dogos_demo.models import AdapterId, DemoError
from dogos_demo.pair_connection import check_pair_connections
from dogos_demo.runtime import DemoRuntime
from dogos_memory.models import SessionId, StoreError

app: Final = typer.Typer(no_args_is_help=True)


def _adapters() -> tuple[SimulationAdapter, SimulationAdapter]:
    return (
        SimulationAdapter(AdapterId("adapter-a")),
        SimulationAdapter(AdapterId("adapter-b")),
    )


@app.command()
def encounter(
    directory: Annotated[Path, typer.Option()],
    session_id: Annotated[str, typer.Option()],
    observed_at_ms: Annotated[int, typer.Option(min=0)],
) -> None:
    """Run one operator-identified, two-step software encounter."""
    runtime = DemoRuntime.initialize(default_simulation_config(), directory, _adapters())
    result = runtime.run_manual(SessionId(session_id), observed_at_ms)
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def status(directory: Annotated[Path, typer.Option()]) -> None:
    """Read both existing stores without initializing missing files."""
    runtime = DemoRuntime.open(default_simulation_config(), directory, _adapters())
    typer.echo(runtime.status().model_dump_json(indent=2))


@app.command("connect-check")
def connect_check(config: Annotated[Path, typer.Option(exists=True, dir_okay=False)]) -> None:
    """Verify both fixed Vbot tunnels and physical identities without sending motion."""
    connection_config = PairConnectionConfig.model_validate_json(config.read_text(encoding="utf-8"))
    report = check_pair_connections(connection_config)
    typer.echo(report.model_dump_json(indent=2))
    match report.state:
        case PairConnectionState.READY:
            return
        case (
            PairConnectionState.DEGRADED
            | PairConnectionState.ROUTING_CONFLICT
            | PairConnectionState.DUPLICATE_DEVICE
        ):
            raise typer.Exit(3)
        case unreachable:
            assert_never(unreachable)


def main() -> None:
    """Translate expected boundary failures to a concise nonzero exit."""
    try:
        app()
    except (DemoError, StoreError, ValidationError, sqlite3.Error, OSError) as error:
        typer.echo(str(error), err=True)
        raise SystemExit(2) from error

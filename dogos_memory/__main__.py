"""CLI for initialization, trusted ingestion, retrieval, and offline demos."""

import sqlite3
from pathlib import Path
from typing import Annotated, Final

import typer
from pydantic import ValidationError

from dogos_memory.demo import read_demo, seed_demo
from dogos_memory.models import DogId, MemoryInput, Profile, RecallRequest, StoreError
from dogos_memory.store import MemoryStore

app: Final = typer.Typer(no_args_is_help=True)


@app.command()
def init(
    database: Annotated[Path, typer.Option()],
    profile_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    """Create/resume a database from an explicit owner profile JSON file."""
    profile = Profile.model_validate_json(profile_file.read_text(encoding="utf-8"))
    _ = MemoryStore.initialize(database, profile)
    typer.echo(profile.model_dump_json(indent=2))


@app.command()
def record(
    database: Annotated[Path, typer.Option()],
    owner: Annotated[str, typer.Option()],
    input_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    """Append a trusted controller event or an evidence-backed impression."""
    observation = MemoryInput.model_validate_json(input_file.read_text(encoding="utf-8"))
    result = MemoryStore(database, DogId(owner)).append(observation)
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def recall(
    database: Annotated[Path, typer.Option()],
    owner: Annotated[str, typer.Option()],
    peer: Annotated[str, typer.Option()],
) -> None:
    """Read the latest confirmed experiences and separate impressions."""
    result = MemoryStore(database, DogId(owner)).recall(RecallRequest(peer_id=DogId(peer)))
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def demo_seed(directory: Annotated[Path, typer.Option()]) -> None:
    """Write a labeled software-only A=70/B=50 scenario; safe to replay."""
    typer.echo(seed_demo(directory).model_dump_json(indent=2))


@app.command()
def demo_recall(directory: Annotated[Path, typer.Option()]) -> None:
    """Read the two existing demo files, without initializing or seeding."""
    typer.echo(read_demo(directory).model_dump_json(indent=2))


def main() -> None:
    """Translate expected boundary errors to a nonzero CLI exit."""
    try:
        app()
    except (StoreError, ValidationError, sqlite3.Error, OSError) as error:
        typer.echo(str(error), err=True)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()

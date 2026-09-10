"""Command-line interface for validating and simulating demo choreography."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import ChoreographyRunner, RoutineBook


DEFAULT_BOOK = Path(__file__).with_name("routines.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Vbot deterministic choreography (dry-run only)")
    parser.add_argument("--book", type=Path, default=DEFAULT_BOOK)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--persona")
    match = subparsers.add_parser("match")
    match.add_argument("value")
    match.add_argument("--kind", choices=("phrase", "button"), default="phrase")
    match.add_argument("--persona")
    run = subparsers.add_parser("run")
    run.add_argument("routine_id")
    run.add_argument("--battery", type=float, default=0)
    run.add_argument("--charging", action="store_true")
    run.add_argument("--moving", action="store_true")
    run.add_argument("--operator-present", action="store_true")
    run.add_argument("--peer-present", action="store_true")
    run.add_argument("--fault", action="store_true")
    args = parser.parse_args()

    book = RoutineBook.load(args.book)
    if args.command == "validate":
        payload: object = {"status": "valid", "routine_count": len(book.routines), "personas": list(book.personas), "hardware": "disabled"}
    elif args.command == "list":
        payload = [item for item in book.list() if args.persona is None or item["persona"] == args.persona]
    elif args.command == "match":
        payload = {
            "kind": args.kind,
            "value": args.value,
            "persona": args.persona,
            "routine_id": book.match(args.kind, args.value, args.persona),
            "matches": book.match_all(args.kind, args.value),
        }
    else:
        payload = ChoreographyRunner(book).run(
            args.routine_id,
            {
                "battery": args.battery,
                "charging": args.charging,
                "stationary": not args.moving,
                "operator_present": args.operator_present,
                "peer_present": args.peer_present,
                "fault": args.fault,
            },
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

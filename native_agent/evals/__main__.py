"""CLI for offline River-log evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .evaluator import EvaluationError, evaluate_log

DEFAULT_SUITE = Path(__file__).parent / "scenarios.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m native_agent.evals")
    parser.add_argument("--log", type=Path, required=True, help="sanitized River JSONL export")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    args = parser.parse_args(argv)
    try:
        report = evaluate_log(args.log, args.suite)
    except EvaluationError as error:
        print(json.dumps({"status": "error", "code": error.code, "detail": error.detail}))
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

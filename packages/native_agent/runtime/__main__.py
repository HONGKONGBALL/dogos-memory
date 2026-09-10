"""Local artifact builder for the Vbot native Agent runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from native_agent.identity.bundle import IdentityError
from native_agent.runtime.artifacts import build_s100_admin, build_x5_capsule, prepare_release


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m native_agent.runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--identity-dir", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--dog-id", required=True)
    admin = commands.add_parser("build-admin")
    admin.add_argument("--output", type=Path, required=True)
    x5 = commands.add_parser("build-x5-installer")
    x5.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_release(args.identity_dir, args.output_dir, args.dog_id)
        elif args.command == "build-admin":
            result = build_s100_admin(args.output)
        else:
            result = build_x5_capsule(args.output)
    except IdentityError as error:
        print(json.dumps({"status": "error", "code": error.code, "detail": error.detail}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

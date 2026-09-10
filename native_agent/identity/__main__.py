"""Command-line interface for identity bundle administration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .bundle import IdentityError, build_bundle, validate_bundle
from .importer import apply_bundle, mark_restart_complete, rollback_bundle
from .staging import pack_staging_envelope, unpack_staging_envelope


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m native_agent.identity")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build an immutable identity bundle")
    build.add_argument("--source-dir", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--dog-id", required=True)

    validate = commands.add_parser("validate", help="validate an existing bundle")
    validate.add_argument("--bundle-dir", type=Path, required=True)

    pack = commands.add_parser("pack", help="pack a bundle into one staging JSON file")
    pack.add_argument("--bundle-dir", type=Path, required=True)
    pack.add_argument("--output-file", type=Path, required=True)

    unpack = commands.add_parser("unpack", help="unpack and validate a staging JSON file")
    unpack.add_argument("--envelope-file", type=Path, required=True)
    unpack.add_argument("--output-dir", type=Path, required=True)

    apply = commands.add_parser("apply", help="dry-run or apply a bundle")
    apply.add_argument("--bundle-dir", type=Path, required=True)
    apply.add_argument("--device-root", type=Path, required=True)
    apply.add_argument("--expected-dog-id")
    apply.add_argument("--execute", action="store_true")
    apply.add_argument("--confirm-live-device")

    rollback = commands.add_parser("rollback", help="dry-run or restore a revision")
    rollback.add_argument("--device-root", type=Path, required=True)
    rollback.add_argument("--revision", required=True)
    rollback.add_argument("--expected-dog-id")
    rollback.add_argument("--execute", action="store_true")
    rollback.add_argument("--confirm-live-device")

    restarted = commands.add_parser(
        "mark-restarted", help="mark an installed revision healthy after restart"
    )
    restarted.add_argument("--device-root", type=Path, required=True)
    restarted.add_argument("--revision", required=True)
    restarted.add_argument("--expected-dog-id")
    restarted.add_argument("--execute", action="store_true")
    restarted.add_argument("--confirm-live-device")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(args.source_dir, args.output_dir, args.dog_id).to_dict()
        elif args.command == "validate":
            result = validate_bundle(args.bundle_dir).to_dict()
        elif args.command == "pack":
            result = pack_staging_envelope(args.bundle_dir, args.output_file)
        elif args.command == "unpack":
            result = unpack_staging_envelope(args.envelope_file, args.output_dir).to_dict()
        elif args.command == "apply":
            result = apply_bundle(
                args.bundle_dir,
                args.device_root,
                execute=args.execute,
                expected_dog_id=args.expected_dog_id,
                confirm_live_device=args.confirm_live_device,
            ).to_dict()
        elif args.command == "rollback":
            result = rollback_bundle(
                args.device_root,
                args.revision,
                execute=args.execute,
                expected_dog_id=args.expected_dog_id,
                confirm_live_device=args.confirm_live_device,
            ).to_dict()
        else:
            result = mark_restart_complete(
                args.device_root,
                args.revision,
                execute=args.execute,
                expected_dog_id=args.expected_dog_id,
                confirm_live_device=args.confirm_live_device,
            ).to_dict()
    except IdentityError as error:
        print(json.dumps({"status": "error", "code": error.code, "detail": error.detail}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

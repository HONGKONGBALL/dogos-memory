"""Run the local Vbot control plane."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .server import create_server, default_data_root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m native_agent.control_plane",
        description="Run the loopback-only Vbot identity preview control plane.",
    )
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    server = create_server(
        port=args.port,
        data_root=args.data_root,
    )
    address, port = server.server_address[:2]
    print(f"Vbot control plane listening on http://{address}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

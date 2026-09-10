"""Read-only state/log witness for the operator's native-dialogue demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from pathlib import Path

from native_agent.runtime.s100_admin import RosAccess, collect_snapshot, _parse_x5_response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("status", "cursor", "read"))
    parser.add_argument("--file", default="")
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    if args.operation == "read" and (
        re.fullmatch(r"river-[0-9]{8}\.jsonl", args.file) is None or args.offset < 0
    ):
        raise SystemExit("Invalid bounded log cursor")
    ros = RosAccess()
    try:
        for _ in range(10):
            ros.rclpy.spin_once(ros.node, timeout_sec=0.1)
        if args.operation == "status":
            snapshot = collect_snapshot(ros, purpose="inspect")
            serial = Path("/proc/device-tree/serial-number").read_text().replace("\0", "").strip().lower()
            mac = Path("/sys/class/net/eth2/address").read_text().strip().lower()
            robot_id = "vbot-" + hashlib.sha256(f"device-tree:{serial}|eth2:{mac}".encode()).hexdigest()[:16]
            print(json.dumps({"robot_id": robot_id, "battery": snapshot["battery"],
                              "faults": snapshot["faults"], "gate": snapshot["gate"],
                              "system": snapshot["context"]["system_status"]}))
            return
        options = {"operation": args.operation, "file": args.file, "offset": args.offset}
        code = """import json,pathlib
options=OPTIONS
root=pathlib.Path('/userdata/.vbot-agent/river-logs')
if options['operation']=='cursor':
 files=[p for p in root.glob('river-*.jsonl') if p.is_file() and not p.is_symlink()]
 p=max(files,key=lambda p:p.stat().st_mtime)
 result={'file':p.name,'offset':p.stat().st_size}
else:
 p=root/options['file']
 if p.is_symlink() or p.resolve().parent!=root.resolve():raise ValueError('unsafe log path')
 with p.open('rb') as handle:
  handle.seek(options['offset']); data=handle.read(262145)
 if len(data)>262144:raise ValueError('log window too large')
 entries=[]
 for line in data.splitlines():
  try:entries.append(json.loads(line))
  except ValueError:pass
 result={'entries':entries}
print(json.dumps(result,ensure_ascii=False))
""".replace("OPTIONS", repr(options))
        response = ros.call("/execute_x5_command", {"command": "python3 -c " + shlex.quote(code)}, timeout=12)
        print(json.dumps(_parse_x5_response(response, operation="dialogue_read"), ensure_ascii=False))
    finally:
        ros.close()


if __name__ == "__main__":
    main()

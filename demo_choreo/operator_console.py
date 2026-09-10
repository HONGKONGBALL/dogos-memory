"""Keyboard-first dry-run and bounded live console for stage routines."""

from __future__ import annotations

import argparse
import json
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .core import ChoreographyRunner, RoutineBook


DEFAULT_BOOK: Final = Path(__file__).with_name("routines.json")


@dataclass(frozen=True, slots=True)
class StageCue:
    key: str
    routine_id: str
    label: str


STAGE_CUES: Final = (
    StageCue("1", "xiaoman_show_identity", "小满 · 谨慎自我介绍"),
    StageCue("2", "buding_show_identity", "布丁 · 主动自我介绍"),
    StageCue("3", "xiaoman_owner_returns", "小满 · 主人回来"),
    StageCue("4", "buding_owner_returns", "布丁 · 主人回来"),
    StageCue("5", "xiaoman_comfort_tired", "小满 · 安静陪伴"),
    StageCue("6", "buding_comfort_tired", "布丁 · 主动打气"),
    StageCue("7", "buding_meets_xiaoman", "布丁 · 先向小满打招呼"),
    StageCue("8", "xiaoman_meets_buding", "小满 · 观察后回应布丁"),
)


def cue_map() -> dict[str, StageCue]:
    return {cue.key: cue for cue in STAGE_CUES}


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "battery": args.battery,
        "charging": args.charging,
        "stationary": not args.moving,
        "operator_present": True,
        "peer_present": args.peer_present,
        "fault": args.fault,
    }


def run_cue(
    key: str,
    runner: ChoreographyRunner,
    context: dict[str, Any],
    persona: str | None = None,
) -> dict[str, Any]:
    cue = cue_map().get(key)
    if cue is None:
        return {"status": "ignored", "reason": "unknown_stage_key", "key": key}
    routine = runner.book.get(cue.routine_id)
    if persona is not None and routine["persona"] != persona:
        return {
            "status": "ignored",
            "reason": "cue_for_other_persona",
            "key": key,
            "expected_persona": persona,
            "cue_persona": routine["persona"],
        }
    result = runner.run(cue.routine_id, context)
    return {"cue": {"key": cue.key, "label": cue.label}, **result}


def print_menu(*, live: bool = False, persona: str | None = None) -> None:
    mode = "LIVE EXPRESSIVE" if live else "DRY-RUN（不会连接机械狗）"
    print(f"\nVbot 舞台控制 · {mode}")
    print("主持人说固定台词，操作员按对应数字键。两狗见面先按 7，再按 8。\n")
    for cue in STAGE_CUES:
        if persona is not None and not cue.routine_id.startswith(persona + "_"):
            continue
        print(f"  [{cue.key}] {cue.label}")
    if live:
        print("  [x] 停止当前编排并清除短时表达输出")
    print("  [q] 退出\n")


def read_stage_key() -> str:
    """Read one key immediately on a TTY, with a line-input fallback."""
    if not sys.stdin.isatty():
        return input("舞台按键 > ").strip().lower()
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        print("舞台按键 > ", end="", flush=True)
        key = sys.stdin.read(1).lower()
        print(key)
        return key
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def print_result(result: dict[str, Any]) -> None:
    if result["status"] == "blocked":
        reasons = ", ".join(result["reasons"])
        print(f"BLOCKED · {result['cue']['label']} · {reasons}")
        return
    if result["status"] == "ignored":
        print("未分配的按键。")
        return
    print(f"{result['status'].upper()} · {result['cue']['label']}")
    for step in result["steps"]:
        detail = step["result"].get("arguments", step["result"])
        arguments = json.dumps(detail, ensure_ascii=False)
        print(
            f"  {step['index'] + 1:02d}. {step['step_id']} · "
            f"{step['action']} · {arguments}"
        )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vbot stage keyboard console")
    parser.add_argument("--book", type=Path, default=DEFAULT_BOOK)
    parser.add_argument("--battery", type=float, default=80)
    parser.add_argument("--charging", action="store_true")
    parser.add_argument("--moving", action="store_true")
    parser.add_argument("--fault", action="store_true")
    parser.add_argument("--peer-present", action="store_true")
    parser.add_argument("--live", action="store_true", help="Use fresh robot telemetry and bounded expression services")
    parser.add_argument("--url", default="ws://127.0.0.1:19091")
    parser.add_argument("--operator-present", action="store_true")
    parser.add_argument("--persona", choices=("xiaoman", "buding"))
    parser.add_argument("--once", choices=tuple(cue_map()), help="Run one numbered cue and exit")
    parser.add_argument("--json", action="store_true", help="Print JSON with --once")
    return parser


def main() -> None:
    args = create_parser().parse_args()
    book = RoutineBook.load(args.book)

    if args.live:
        run_live(args, book)
        return

    runner = ChoreographyRunner(book)

    if args.once:
        result = run_cue(args.once, runner, build_context(args))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_result(result)
        return

    print_menu()
    while True:
        try:
            key = read_stage_key()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if key == "q":
            return
        print_result(run_cue(key, runner, build_context(args)))


def run_live(args: argparse.Namespace, book: RoutineBook) -> None:
    from .live import LiveDriver, LiveStateProbe, append_audit

    if not args.operator_present:
        raise SystemExit("LIVE blocked: start with --operator-present while the operator is physically beside the robot")
    if args.persona is None:
        raise SystemExit("LIVE blocked: select exactly one device persona with --persona xiaoman or --persona buding")

    probe = LiveStateProbe(url=args.url)
    report = probe.report(
        operator_present=args.operator_present,
        peer_present=args.peer_present,
    )
    if report["status"] != "ready":
        probe.close()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit("LIVE blocked: fresh battery/context telemetry is unavailable")

    def progress(phase: str, value: dict[str, Any]) -> None:
        step_id = value.get("step_id", "")
        action = value.get("action", "")
        print(f"[{phase}] {step_id} · {action}")

    driver = LiveDriver(
        url=args.url,
        progress=progress,
        guard=lambda: probe.context(
            operator_present=args.operator_present,
            peer_present=args.peer_present,
        ),
    )
    runner = ChoreographyRunner(book, driver=driver)
    worker: threading.Thread | None = None
    worker_lock = threading.Lock()
    last_started: dict[str, float] = {}

    def execute_key(key: str) -> None:
        nonlocal worker
        try:
            cue = cue_map()[key]
            routine = book.get(cue.routine_id)
            driver.begin_routine(
                routine["max_duration_ms"],
                min_battery=routine["preconditions"]["min_battery"],
            )
            context = probe.context(
                operator_present=args.operator_present,
                peer_present=args.peer_present,
            )
            result = run_cue(key, runner, context, persona=args.persona)
            append_audit({"kind": "routine", "context": context, "result": result})
            print_result(result)
        finally:
            with worker_lock:
                worker = None

    try:
        if args.once:
            execute_key(args.once)
            return

        print(json.dumps(report, ensure_ascii=False, indent=2))
        print_menu(live=True, persona=args.persona)
        while True:
            key = read_stage_key()
            if key == "x":
                stop_result = driver.request_stop()
                append_audit({"kind": "operator_stop", "result": stop_result})
                print("停止请求已发送；它不是整机急停。")
                continue
            if key == "q":
                with worker_lock:
                    active = worker
                if active is not None and active.is_alive():
                    driver.request_stop()
                    active.join(timeout=10)
                return
            if key not in cue_map():
                print("未分配的按键。")
                continue
            with worker_lock:
                if worker is not None and worker.is_alive():
                    print("当前编排仍在运行；按 x 停止。")
                    continue
                now = time.monotonic()
                if now - last_started.get(key, float("-inf")) < 3:
                    print("已忽略 3 秒内的重复触发。")
                    continue
                last_started[key] = now
                worker = threading.Thread(target=execute_key, args=(key,), daemon=True)
                worker.start()
    except (EOFError, KeyboardInterrupt):
        driver.request_stop()
    finally:
        probe.close()


if __name__ == "__main__":
    main()

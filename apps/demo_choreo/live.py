"""Bounded live runtime for the Vbot stage choreography.

Only the already exposed expression services are dispatched. Native body DAGs
remain visible in the trace but are skipped until a separate physical
verification and cancellation path exists.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Final

from companion.native import NativeClient
from companion.sensors import Sensors

from .core import ExecutionCancelled


DEFAULT_URL: Final = "ws://127.0.0.1:19091"
DEFAULT_AUDIT_LOG: Final = Path(__file__).with_name(".runtime") / "stage-runs.jsonl"


def unavailable_context(*, operator_present: bool, peer_present: bool) -> dict[str, Any]:
    return {
        "battery": None,
        "charging": None,
        "stationary": False,
        "operator_present": operator_present,
        "peer_present": peer_present,
        "fault": True,
    }


class LiveStateProbe:
    """Read fresh battery and context topics before every routine."""

    def __init__(self, url: str = DEFAULT_URL, *, sensors: Sensors | None = None):
        self.url = url
        self.sensors = sensors or Sensors(url=url)
        self.started = False

    def start(self) -> None:
        if not self.started:
            self.sensors.start()
            self.started = True

    def wait_ready(self, timeout_seconds: float = 10.0) -> bool:
        self.start()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.sensors.observation(max_age=2.0) is not None:
                return True
            time.sleep(0.1)
        return False

    def context(self, *, operator_present: bool, peer_present: bool) -> dict[str, Any]:
        observation = self.sensors.observation(max_age=2.0)
        if observation is None:
            return unavailable_context(
                operator_present=operator_present,
                peer_present=peer_present,
            )
        return {
            "battery": observation["battery"],
            "charging": observation["charging"],
            "stationary": not observation["busy"],
            "operator_present": operator_present,
            "peer_present": peer_present,
            "fault": bool(observation["busy"]),
        }

    def report(self, *, operator_present: bool, peer_present: bool) -> dict[str, Any]:
        ready = self.wait_ready()
        context = self.context(
            operator_present=operator_present,
            peer_present=peer_present,
        )
        return {
            "status": "ready" if ready else "blocked",
            "bridge_url": self.url,
            "context": context,
            "sensor_status": self.sensors.status(),
            "live_scope": ["head", "emotion", "light", "say", "wait"],
            "native_dag": "skipped_not_enabled",
        }

    def close(self) -> None:
        self.sensors.stop.set()


class LiveDriver:
    """Dispatch short expression services with interruptible pacing."""

    final_status = "completed"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        client: NativeClient | None = None,
        resources: dict[str, Any] | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
        wait: Callable[[float], bool] | None = None,
        guard: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client or NativeClient(url=url)
        self.resources = resources if resources is not None else self.client.call("/datou/resources")
        self.progress = progress or (lambda _phase, _value: None)
        self.cancel_event = threading.Event()
        self.wait = wait or self.cancel_event.wait
        self.guard = guard
        self.clock = clock
        self.deadline: float | None = None
        self.min_battery = 25.0
        self.active_emotion_mode: int | None = None

    def clear_cancel(self) -> None:
        self.cancel_event.clear()

    def begin_routine(self, max_duration_ms: int, *, min_battery: float = 25.0) -> None:
        self.clear_cancel()
        self.deadline = self.clock() + max_duration_ms / 1000
        self.min_battery = max(25.0, float(min_battery))
        self.active_emotion_mode = None

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise ExecutionCancelled("operator_stop_requested")
        if self.deadline is not None and self.clock() >= self.deadline:
            raise RuntimeError("routine_timeout")

    def _pace(self, seconds: float) -> None:
        self._check_cancelled()
        remaining = seconds
        if self.deadline is not None:
            remaining = min(remaining, max(0.0, self.deadline - self.clock()))
        if remaining > 0 and self.wait(remaining):
            raise ExecutionCancelled("operator_stop_requested")
        if remaining < seconds:
            raise RuntimeError("routine_timeout")

    def execute(self, step: dict[str, Any]) -> dict[str, Any]:
        self._check_cancelled()
        action = step["action"]
        args = step["args"]
        self.progress("started", {"step_id": step["id"], "action": action})

        if action == "native_dag":
            result = {
                "status": "skipped",
                "reason": "native_dag_not_enabled",
                "task_id": args["task_id"],
                "source_file": args["source_file"],
            }
        elif action == "wait":
            self._pace(args["duration_ms"] / 1000)
            result = {"status": "completed", "duration_ms": args["duration_ms"]}
        elif action == "head":
            self._guard_expression()
            result = self.client.head(args["pitch"], args["yaw"])
            self._pace(max(1.0, args["duration_ms"] / 1000))
        elif action == "light":
            self._guard_expression()
            result = self.client.light(
                tuple(args["rgb"]),
                brightness=args["brightness"],
                duration_ms=args["duration_ms"],
            )
            self._pace(args["duration_ms"] / 1000)
        elif action == "emotion":
            self._guard_expression()
            emotions = self.resources.get("emotions", {})
            name = next((value for value in args["candidates"] if value in emotions), None)
            if name is None:
                result = {
                    "status": "skipped",
                    "reason": "emotion_resource_unavailable",
                    "candidates": args["candidates"],
                }
            else:
                self.active_emotion_mode = emotions[name]
                result = self.client.emotion(
                    self.active_emotion_mode,
                    duration_ms=args["duration_ms"],
                )
                result = {**result, "resource": name}
                self._pace(args["duration_ms"] / 1000)
        elif action == "say":
            self._guard_expression()
            result = self.client.say(args["text"])
            self._pace(min(5.0, 0.8 + len(args["text"]) * 0.12))
        else:  # RoutineBook validation should make this unreachable.
            raise RuntimeError(f"unsupported_live_action:{action}")

        self.progress("finished", {"step_id": step["id"], "action": action, "result": result})
        return result

    def _guard_expression(self) -> None:
        if self.guard is None:
            return
        context = self.guard()
        reasons = []
        battery = context.get("battery")
        if type(battery) not in (int, float) or battery < self.min_battery:
            reasons.append(f"battery_below_{self.min_battery:g}_or_unknown")
        if context.get("charging") is not False:
            reasons.append("charging_or_unknown")
        if context.get("stationary") is not True:
            reasons.append("robot_not_confirmed_stationary")
        if context.get("operator_present") is not True:
            reasons.append("operator_not_present")
        if context.get("fault") is not False:
            reasons.append("fault_or_unknown")
        if reasons:
            raise RuntimeError("live_guard_blocked:" + ",".join(reasons))

    def request_stop(self) -> list[dict[str, Any]]:
        """Stop the sequence and best-effort clear short expression outputs."""
        self.cancel_event.set()
        self.deadline = None
        results = self.client.stop_expression(self.active_emotion_mode)
        try:
            results.append(self.client.head(0.0, 0.0))
        except Exception as error:
            results.append({"status": "stop_failed", "error": str(error)})
        return results


def append_audit(value: dict[str, Any], path: Path = DEFAULT_AUDIT_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **value}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Vbot live stage bridge utilities")
    parser.add_argument("command", choices=("preflight", "stop"))
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--operator-present", action="store_true")
    parser.add_argument("--peer-present", action="store_true")
    args = parser.parse_args()

    if args.command == "stop":
        result = LiveDriver(url=args.url, resources={"emotions": {}}).request_stop()
        print(json.dumps({"status": "stop_requested", "results": result}, ensure_ascii=False, indent=2))
        return

    probe = LiveStateProbe(url=args.url)
    try:
        print(
            json.dumps(
                probe.report(
                    operator_present=args.operator_present,
                    peer_present=args.peer_present,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        probe.close()


if __name__ == "__main__":
    main()

"""Validate and simulate fixed Vbot demo routines without touching hardware."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol


class ValidationError(ValueError):
    """Raised when a choreography file can produce an unsafe or ambiguous run."""


class ExecutionCancelled(RuntimeError):
    """Raised by a driver when the operator stops the current routine."""


def _is_number(value: object) -> bool:
    return type(value) in (int, float)


def normalize_phrase(value: str) -> str:
    """Normalize only whitespace and terminal punctuation; matching stays exact."""
    return re.sub(r"[。！？，、.!?,]+$", "", " ".join(value.strip().split())).casefold()


class Driver(Protocol):
    def execute(self, step: dict[str, Any]) -> dict[str, Any]: ...


class DryRunDriver:
    """Return the planned action. This driver has no hardware dependency."""

    final_status = "simulated"

    def execute(self, step: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "simulated",
            "action": step["action"],
            "arguments": step.get("args", {}),
            "verification": step.get("verification", "not_run"),
        }


class RoutineBook:
    """Immutable validated routine catalogue with exact trigger matching."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self._validate()
        self.routines = {routine["id"]: routine for routine in payload["routines"]}
        self.personas = payload["personas"]
        self._triggers: dict[tuple[str, str, str], str] = {}
        for routine in payload["routines"]:
            for trigger in routine["triggers"]:
                value = trigger["value"]
                if trigger["kind"] == "phrase":
                    value = normalize_phrase(value)
                self._triggers[(routine["persona"], trigger["kind"], value)] = routine["id"]

    @classmethod
    def load(cls, path: Path) -> "RoutineBook":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValidationError("Routine book must be a JSON object")
        return cls(payload)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": routine["id"],
                "label": routine["label"],
                "persona": routine["persona"],
                "step_count": len(routine["steps"]),
                "triggers": routine["triggers"],
            }
            for routine in self.payload["routines"]
        ]

    def match(self, kind: str, value: str, persona: str | None = None) -> str | None:
        key = normalize_phrase(value) if kind == "phrase" else value
        if persona is not None:
            return self._triggers.get((persona, kind, key))
        matches = [routine_id for (candidate, trigger_kind, trigger_value), routine_id in self._triggers.items()
                   if trigger_kind == kind and trigger_value == key and candidate in self.personas]
        return matches[0] if len(matches) == 1 else None

    def match_all(self, kind: str, value: str) -> list[dict[str, str]]:
        key = normalize_phrase(value) if kind == "phrase" else value
        return [
            {"persona": persona, "routine_id": routine_id}
            for (persona, trigger_kind, trigger_value), routine_id in self._triggers.items()
            if trigger_kind == kind and trigger_value == key
        ]

    def get(self, routine_id: str) -> dict[str, Any]:
        try:
            return self.routines[routine_id]
        except KeyError as error:
            raise ValidationError(f"Unknown routine: {routine_id}") from error

    def _validate(self) -> None:
        if self.payload.get("schema_version") != 2:
            raise ValidationError("schema_version must be 2")
        personas = self.payload.get("personas")
        if not isinstance(personas, dict) or not personas:
            raise ValidationError("personas must be a non-empty object")
        for persona_id, profile in personas.items():
            if not isinstance(persona_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", persona_id):
                raise ValidationError("Persona id must be stable snake_case")
            if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
                raise ValidationError(f"{persona_id}: persona name is required")
            intensity = profile.get("max_intensity")
            if not _is_number(intensity) or not 0 < intensity <= 1:
                raise ValidationError(f"{persona_id}: max_intensity must be 0-1")
        routines = self.payload.get("routines")
        if not isinstance(routines, list) or not routines:
            raise ValidationError("routines must be a non-empty list")
        ids: set[str] = set()
        triggers: set[tuple[str, str]] = set()
        for routine in routines:
            self._validate_routine(routine, ids, triggers)

    def _validate_routine(
        self,
        routine: object,
        ids: set[str],
        triggers: set[tuple[str, str]],
    ) -> None:
        if not isinstance(routine, dict):
            raise ValidationError("Each routine must be an object")
        routine_id = routine.get("id")
        if not isinstance(routine_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", routine_id):
            raise ValidationError("Routine id must be stable snake_case")
        if routine_id in ids:
            raise ValidationError(f"Duplicate routine id: {routine_id}")
        ids.add(routine_id)
        if not isinstance(routine.get("label"), str) or not routine["label"].strip():
            raise ValidationError(f"{routine_id}: label is required")
        persona = routine.get("persona")
        if persona not in self.payload["personas"]:
            raise ValidationError(f"{routine_id}: unknown persona {persona}")
        max_duration = routine.get("max_duration_ms")
        if type(max_duration) is not int or not 1000 <= max_duration <= 30000:
            raise ValidationError(f"{routine_id}: max_duration_ms must be 1000-30000")
        preconditions = routine.get("preconditions")
        if not isinstance(preconditions, dict):
            raise ValidationError(f"{routine_id}: preconditions are required")
        min_battery = preconditions.get("min_battery")
        if not _is_number(min_battery) or not 0 <= min_battery <= 100:
            raise ValidationError(f"{routine_id}: invalid min_battery")
        for name in ("require_operator", "require_stationary", "reject_charging", "require_peer"):
            if type(preconditions.get(name)) is not bool:
                raise ValidationError(f"{routine_id}: {name} must be boolean")
        routine_triggers = routine.get("triggers")
        if not isinstance(routine_triggers, list) or not routine_triggers:
            raise ValidationError(f"{routine_id}: at least one trigger is required")
        for trigger in routine_triggers:
            if not isinstance(trigger, dict) or trigger.get("kind") not in ("phrase", "button"):
                raise ValidationError(f"{routine_id}: trigger kind must be phrase or button")
            value = trigger.get("value")
            if not isinstance(value, str) or not value.strip() or len(value) > 80:
                raise ValidationError(f"{routine_id}: invalid trigger value")
            key_value = normalize_phrase(value) if trigger["kind"] == "phrase" else value
            key = (persona, trigger["kind"], key_value)
            if key in triggers:
                raise ValidationError(f"Duplicate trigger: {key}")
            triggers.add(key)
        steps = routine.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
            raise ValidationError(f"{routine_id}: steps must contain 1-12 entries")
        step_ids: set[str] = set()
        estimate = 0
        for step in steps:
            estimate += self._validate_step(routine_id, step, step_ids)
        if estimate > max_duration:
            raise ValidationError(
                f"{routine_id}: estimated duration {estimate} exceeds {max_duration}"
            )

    @staticmethod
    def _validate_step(routine_id: str, step: object, step_ids: set[str]) -> int:
        if not isinstance(step, dict):
            raise ValidationError(f"{routine_id}: each step must be an object")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", step_id):
            raise ValidationError(f"{routine_id}: invalid step id")
        if step_id in step_ids:
            raise ValidationError(f"{routine_id}: duplicate step id {step_id}")
        step_ids.add(step_id)
        action = step.get("action")
        if action not in ("say", "head", "emotion", "light", "wait", "native_dag"):
            raise ValidationError(f"{routine_id}/{step_id}: unsupported action {action}")
        if step.get("on_failure", "abort") not in ("abort", "continue"):
            raise ValidationError(f"{routine_id}/{step_id}: invalid on_failure")
        args = step.get("args")
        if not isinstance(args, dict):
            raise ValidationError(f"{routine_id}/{step_id}: args must be an object")
        if action == "say":
            text = args.get("text")
            if not isinstance(text, str) or not 1 <= len(text) <= 120:
                raise ValidationError(f"{routine_id}/{step_id}: say text must be 1-120 chars")
            return min(5000, 800 + len(text) * 120)
        if action == "head":
            pitch, yaw = args.get("pitch"), args.get("yaw")
            duration = args.get("duration_ms")
            if not _is_number(pitch) or not _is_number(yaw) or abs(pitch) > 0.12 or abs(yaw) > 0.12:
                raise ValidationError(f"{routine_id}/{step_id}: head angle exceeds 0.12 rad")
            if type(duration) is not int or not 100 <= duration <= 1500:
                raise ValidationError(f"{routine_id}/{step_id}: invalid head duration")
            return duration
        if action == "emotion":
            candidates = args.get("candidates")
            duration = args.get("duration_ms")
            if (
                not isinstance(candidates, list)
                or not candidates
                or any(not isinstance(value, str) or not value for value in candidates)
            ):
                raise ValidationError(f"{routine_id}/{step_id}: emotion candidates are required")
            if type(duration) is not int or not 100 <= duration <= 3000:
                raise ValidationError(f"{routine_id}/{step_id}: invalid emotion duration")
            return duration
        if action == "light":
            rgb = args.get("rgb")
            brightness = args.get("brightness")
            duration = args.get("duration_ms")
            if not isinstance(rgb, list) or len(rgb) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in rgb):
                raise ValidationError(f"{routine_id}/{step_id}: rgb must contain three bytes")
            if type(brightness) is not int or not 1 <= brightness <= 50:
                raise ValidationError(f"{routine_id}/{step_id}: brightness must be 1-50")
            if type(duration) is not int or not 100 <= duration <= 3000:
                raise ValidationError(f"{routine_id}/{step_id}: invalid light duration")
            return duration
        if action == "wait":
            duration = args.get("duration_ms")
            if type(duration) is not int or not 0 <= duration <= 3000:
                raise ValidationError(f"{routine_id}/{step_id}: wait must be 0-3000 ms")
            return duration
        task_id = args.get("task_id")
        source_file = args.get("source_file")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,63}", task_id):
            raise ValidationError(f"{routine_id}/{step_id}: invalid native DAG task_id")
        if not isinstance(source_file, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,64}\.json", source_file):
            raise ValidationError(f"{routine_id}/{step_id}: invalid native DAG source_file")
        if step.get("verification") != "not_run":
            raise ValidationError(f"{routine_id}/{step_id}: native DAG must remain not_run")
        return 5000


class ChoreographyRunner:
    """Run one validated routine through an injected driver."""

    def __init__(self, book: RoutineBook, driver: Driver | None = None):
        self.book = book
        self.driver = driver or DryRunDriver()

    def run(self, routine_id: str, context: dict[str, Any]) -> dict[str, Any]:
        routine = self.book.get(routine_id)
        blocked = self._blocked_reasons(routine["preconditions"], context)
        if blocked:
            return {"routine_id": routine_id, "persona": routine["persona"], "status": "blocked", "reasons": blocked, "steps": []}
        results: list[dict[str, Any]] = []
        for index, step in enumerate(routine["steps"]):
            try:
                result = self.driver.execute(step)
                results.append({
                    "index": index,
                    "step_id": step["id"],
                    "action": step["action"],
                    "result": result,
                })
            except ExecutionCancelled as error:
                results.append({
                    "index": index,
                    "step_id": step["id"],
                    "action": step["action"],
                    "result": {"status": "cancelled", "error": str(error)},
                })
                return {
                    "routine_id": routine_id,
                    "persona": routine["persona"],
                    "status": "cancelled",
                    "steps": results,
                }
            except Exception as error:  # Driver errors are part of the demo trace.
                results.append({
                    "index": index,
                    "step_id": step["id"],
                    "action": step["action"],
                    "result": {"status": "failed", "error": str(error)},
                })
                if step.get("on_failure", "abort") == "abort":
                    return {"routine_id": routine_id, "persona": routine["persona"], "status": "failed", "steps": results}
        final_status = getattr(self.driver, "final_status", "completed")
        if any(step["result"].get("status") == "skipped" for step in results):
            final_status = f"{final_status}_with_skips"
        return {
            "routine_id": routine_id,
            "persona": routine["persona"],
            "status": final_status,
            "steps": results,
        }

    @staticmethod
    def _blocked_reasons(preconditions: dict[str, Any], context: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        battery = context.get("battery")
        if not _is_number(battery) or battery < preconditions["min_battery"]:
            reasons.append("battery_below_minimum_or_unknown")
        if preconditions["reject_charging"] and context.get("charging") is not False:
            reasons.append("charging_or_unknown")
        if preconditions["require_stationary"] and context.get("stationary") is not True:
            reasons.append("robot_not_confirmed_stationary")
        if preconditions["require_operator"] and context.get("operator_present") is not True:
            reasons.append("operator_not_present")
        if preconditions["require_peer"] and context.get("peer_present") is not True:
            reasons.append("peer_not_present")
        if context.get("fault") is not False:
            reasons.append("fault_or_unknown")
        return reasons

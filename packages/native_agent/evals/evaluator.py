"""Evaluate a sanitized export of the native Agent's River JSONL log."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

EVAL_ID = re.compile(r"\[eval:([a-z0-9_-]+)\]")
MAX_SUITE_BYTES = 256 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_LOG_LINE_CHARS = 256 * 1024
MAX_TURN_TEXT_CHARS = 64 * 1024


class EvaluationError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    prompt: str
    expected_text_any: tuple[str, ...]
    forbidden_text_any: tuple[str, ...]
    required_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    passed: bool
    text: str
    tools: tuple[str, ...]
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        encoded = self.text.encode("utf-8")
        return {
            "scenario_id": self.scenario_id,
            "passed": self.passed,
            "response": {
                "redacted": True,
                "chars": len(self.text),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            },
            "tools": list(self.tools),
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    suite_id: str
    passed: bool
    results: tuple[ScenarioResult, ...]

    def to_dict(self) -> dict[str, object]:
        failed = [result.scenario_id for result in self.results if not result.passed]
        return {
            "status": "success" if self.passed else "error",
            "summary": f"{len(self.results) - len(failed)}/{len(self.results)} scenarios passed",
            "next_actions": [] if not failed else [f"rerun failed scenarios: {', '.join(failed)}"],
            "artifacts": [],
            "suite_id": self.suite_id,
            "results": [result.to_dict() for result in self.results],
        }


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EvaluationError("invalid_suite", label)
    return tuple(value)


def load_suite(path: Path) -> tuple[str, tuple[Scenario, ...]]:
    if path.is_symlink() or not path.is_file():
        raise EvaluationError("invalid_suite", "suite must be a regular file")
    if path.stat().st_size > MAX_SUITE_BYTES:
        raise EvaluationError("invalid_suite", "suite is too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationError("invalid_suite", str(error)) from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "suite_id", "scenarios"}:
        raise EvaluationError("invalid_suite", "unexpected root shape")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(value["suite_id"], str):
        raise EvaluationError("invalid_suite", "schema_version or suite_id")
    raw_scenarios = value["scenarios"]
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise EvaluationError("invalid_suite", "scenarios")
    scenarios: list[Scenario] = []
    seen: set[str] = set()
    expected_keys = {
        "id",
        "prompt",
        "expected_text_any",
        "forbidden_text_any",
        "required_tools",
        "forbidden_tools",
    }
    for item in raw_scenarios:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise EvaluationError("invalid_suite", "scenario shape")
        scenario_id = item["id"]
        prompt = item["prompt"]
        if (
            not isinstance(scenario_id, str)
            or not re.fullmatch(r"[a-z0-9_-]+", scenario_id)
            or scenario_id in seen
            or not isinstance(prompt, str)
            or f"[eval:{scenario_id}]" not in prompt
        ):
            raise EvaluationError("invalid_suite", str(scenario_id))
        seen.add(scenario_id)
        scenarios.append(
            Scenario(
                scenario_id=scenario_id,
                prompt=prompt,
                expected_text_any=_string_list(item["expected_text_any"], "expected_text_any"),
                forbidden_text_any=_string_list(item["forbidden_text_any"], "forbidden_text_any"),
                required_tools=_string_list(item["required_tools"], "required_tools"),
                forbidden_tools=_string_list(item["forbidden_tools"], "forbidden_tools"),
            )
        )
    return value["suite_id"], tuple(scenarios)


@dataclass(slots=True)
class _ObservedTurn:
    text: list[str]
    tools: list[str]
    text_chars: int = 0


def _extract_interact_text(arguments: object) -> str:
    if not isinstance(arguments, str):
        return ""
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        return ""
    if not isinstance(value, dict) or not isinstance(value.get("segments"), list):
        return ""
    pieces = []
    for segment in value["segments"]:
        if isinstance(segment, dict) and isinstance(segment.get("text"), str):
            pieces.append(segment["text"])
    return " ".join(pieces)


def _read_turns(path: Path) -> Mapping[str, _ObservedTurn]:
    turns: dict[str, _ObservedTurn] = {}
    current: str | None = None
    if path.is_symlink() or not path.is_file():
        raise EvaluationError("invalid_log", "log must be a regular file")
    if path.stat().st_size > MAX_LOG_BYTES:
        raise EvaluationError("invalid_log", "log is too large")
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if len(line) > MAX_LOG_LINE_CHARS:
                    raise EvaluationError("invalid_log", f"line {line_number} is too large")
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as error:
                    raise EvaluationError("invalid_log", f"line {line_number}: invalid JSON") from error
                if not isinstance(entry, dict) or not isinstance(entry.get("kind"), dict):
                    continue
                kind = entry["kind"]
                if "UserText" in kind and isinstance(kind["UserText"], dict):
                    content = kind["UserText"].get("content", "")
                    if isinstance(content, str):
                        marker = EVAL_ID.search(content)
                        if marker:
                            current = marker.group(1)
                            if current in turns:
                                raise EvaluationError("duplicate_scenario", current)
                            turns[current] = _ObservedTurn([], [])
                elif current is not None and "AssistantText" in kind and isinstance(kind["AssistantText"], dict):
                    content = kind["AssistantText"].get("content")
                    if isinstance(content, str):
                        turns[current].text.append(content)
                        turns[current].text_chars += len(content)
                elif current is not None and "ToolCall" in kind and isinstance(kind["ToolCall"], dict):
                    name = kind["ToolCall"].get("name")
                    if isinstance(name, str):
                        turns[current].tools.append(name)
                        if name == "interact":
                            text = _extract_interact_text(kind["ToolCall"].get("arguments"))
                            if text:
                                turns[current].text.append(text)
                                turns[current].text_chars += len(text)
                if current is not None and turns[current].text_chars > MAX_TURN_TEXT_CHARS:
                    raise EvaluationError("invalid_log", f"scenario {current} response is too large")
    except (OSError, UnicodeDecodeError) as error:
        raise EvaluationError("invalid_log", type(error).__name__) from error
    return turns


def evaluate_log(log_path: Path, suite_path: Path) -> EvaluationReport:
    suite_id, scenarios = load_suite(suite_path)
    observed = _read_turns(log_path)
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        turn = observed.get(scenario.scenario_id)
        if turn is None:
            results.append(
                ScenarioResult(scenario.scenario_id, False, "", (), ("scenario missing from log",))
            )
            continue
        text = " ".join(turn.text)
        folded = text.casefold()
        failures: list[str] = []
        if scenario.expected_text_any and not any(
            value.casefold() in folded for value in scenario.expected_text_any
        ):
            failures.append("none of expected_text_any appeared")
        forbidden_text = [value for value in scenario.forbidden_text_any if value.casefold() in folded]
        if forbidden_text:
            failures.append("forbidden text detected")
        missing_tools = [tool for tool in scenario.required_tools if tool not in turn.tools]
        if missing_tools:
            failures.append(f"missing tools: {', '.join(missing_tools)}")
        forbidden_tools = [tool for tool in scenario.forbidden_tools if tool in turn.tools]
        if forbidden_tools:
            failures.append(f"forbidden tools: {', '.join(forbidden_tools)}")
        results.append(
            ScenarioResult(
                scenario.scenario_id,
                not failures,
                text,
                tuple(turn.tools),
                tuple(failures),
            )
        )
    return EvaluationReport(suite_id, all(result.passed for result in results), tuple(results))

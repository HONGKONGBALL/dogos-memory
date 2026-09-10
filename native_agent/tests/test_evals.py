from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from native_agent.evals.evaluator import (
    MAX_LOG_BYTES,
    EvaluationError,
    evaluate_log,
    load_suite,
)

SUITE = Path(__file__).parents[1] / "evals" / "scenarios.json"


def entry(kind: dict[str, object]) -> str:
    return json.dumps({"kind": kind}, ensure_ascii=False)


class AgentEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.log = Path(self.temporary.name) / "river.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_passing_log(self) -> None:
        rows = [
            entry({"UserText": {"content": "[eval:identity_name] who"}}),
            entry({"AssistantText": {"content": "我叫 Harbor。"}}),
            entry({"UserText": {"content": "[eval:owner_fact] owner"}}),
            entry({"AssistantText": {"content": "我会称呼主人 Chen。"}}),
            entry({"UserText": {"content": "[eval:unknown_color] color"}}),
            entry({"AssistantText": {"content": "没有记录，所以我不知道。"}}),
            entry({"UserText": {"content": "[eval:battery_tool] battery"}}),
            entry({"ToolCall": {"name": "query_battery", "arguments": "{}"}}),
            entry(
                {
                    "ToolCall": {
                        "name": "interact",
                        "arguments": json.dumps({"segments": [{"text": "电量是 42%。"}]}),
                    }
                }
            ),
            entry({"UserText": {"content": "[eval:memory_view] memory"}}),
            entry({"ToolCall": {"name": "memory_manage", "arguments": '{"action":"view"}'}}),
            entry({"AssistantText": {"content": "第一次见面在海边图书馆。"}}),
        ]
        self.log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_suite_is_well_formed(self) -> None:
        suite_id, scenarios = load_suite(SUITE)
        self.assertEqual(suite_id, "identity-only-v1")
        self.assertEqual(len(scenarios), 5)

    def test_passing_log_reports_all_scenarios(self) -> None:
        self.write_passing_log()
        report = evaluate_log(self.log, SUITE)
        self.assertTrue(report.passed)
        output = report.to_dict()
        self.assertEqual(output["summary"], "5/5 scenarios passed")
        self.assertNotIn("Harbor", json.dumps(output, ensure_ascii=False))
        self.assertTrue(output["results"][0]["response"]["redacted"])

    def test_forbidden_motion_and_wrong_identity_fail(self) -> None:
        self.write_passing_log()
        rows = self.log.read_text(encoding="utf-8").splitlines()
        rows[1] = entry({"AssistantText": {"content": "我是大头啵啵。"}})
        rows.insert(2, entry({"ToolCall": {"name": "jump", "arguments": "{}"}}))
        self.log.write_text("\n".join(rows) + "\n", encoding="utf-8")
        report = evaluate_log(self.log, SUITE)
        identity = report.results[0]
        self.assertFalse(report.passed)
        self.assertFalse(identity.passed)
        self.assertIn("jump", identity.tools)
        self.assertTrue(any("forbidden" in failure for failure in identity.failures))

    def test_missing_scenario_and_invalid_json_are_reported(self) -> None:
        self.log.write_text(entry({"UserText": {"content": "[eval:identity_name]"}}) + "\n", encoding="utf-8")
        report = evaluate_log(self.log, SUITE)
        self.assertFalse(report.passed)
        self.assertTrue(any("missing" in result.failures[0] for result in report.results[1:]))
        self.log.write_text("not json\n", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "invalid_log"):
            evaluate_log(self.log, SUITE)

    def test_symlink_and_oversized_log_are_rejected(self) -> None:
        target = Path(self.temporary.name) / "target.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        self.log.symlink_to(target)
        with self.assertRaisesRegex(EvaluationError, "regular file"):
            evaluate_log(self.log, SUITE)
        self.log.unlink()
        with self.log.open("wb") as handle:
            handle.truncate(MAX_LOG_BYTES + 1)
        with self.assertRaisesRegex(EvaluationError, "too large"):
            evaluate_log(self.log, SUITE)


if __name__ == "__main__":
    unittest.main()

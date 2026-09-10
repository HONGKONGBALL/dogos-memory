import argparse
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from demo_choreo import ChoreographyRunner, RoutineBook
from demo_choreo.operator_console import build_context, cue_map, print_result, run_cue


BOOK = Path(__file__).resolve().parents[1] / "routines.json"


class OperatorConsoleTests(unittest.TestCase):
    def setUp(self):
        self.runner = ChoreographyRunner(RoutineBook.load(BOOK))
        self.args = argparse.Namespace(
            battery=80,
            charging=False,
            moving=False,
            fault=False,
            peer_present=False,
        )

    def test_all_eight_keys_map_to_unique_routines(self):
        cues = cue_map()
        self.assertEqual(set(cues), set("12345678"))
        self.assertEqual(len({cue.routine_id for cue in cues.values()}), 8)

    def test_pair_meeting_order_is_buding_then_xiaoman(self):
        cues = cue_map()
        self.assertEqual(cues["7"].routine_id, "buding_meets_xiaoman")
        self.assertEqual(cues["8"].routine_id, "xiaoman_meets_buding")

    def test_charging_blocks_a_stage_cue(self):
        context = build_context(self.args)
        context["charging"] = True
        result = run_cue("1", self.runner, context)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("charging_or_unknown", result["reasons"])

    def test_unknown_key_is_ignored(self):
        result = run_cue("9", self.runner, build_context(self.args))
        self.assertEqual(result["status"], "ignored")

    def test_live_persona_lock_rejects_the_other_dog_cue(self):
        result = run_cue(
            "2",
            self.runner,
            build_context(self.args),
            persona="xiaoman",
        )
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "cue_for_other_persona")

    def test_live_result_prints_step_action_from_trace(self):
        result = {
            "status": "completed",
            "cue": {"label": "live"},
            "steps": [{
                "index": 0,
                "step_id": "line",
                "action": "say",
                "result": {"status": "service_acknowledged"},
            }],
        }
        output = io.StringIO()
        with redirect_stdout(output):
            print_result(result)
        self.assertIn("say", output.getvalue())


if __name__ == "__main__":
    unittest.main()

import copy
import json
import unittest
from pathlib import Path

from demo_choreo import ChoreographyRunner, RoutineBook, ValidationError


BOOK = Path(__file__).resolve().parents[1] / "routines.json"
CAPABILITIES = BOOK.parents[1] / "companion" / "capabilities.json"


class ChoreographyTests(unittest.TestCase):
    def setUp(self):
        self.book = RoutineBook.load(BOOK)
        self.ready = {
            "battery": 80,
            "charging": False,
            "stationary": True,
            "operator_present": True,
            "peer_present": False,
            "fault": False,
        }

    def test_default_book_has_two_personas_and_eight_routines(self):
        self.assertEqual(set(self.book.personas), {"xiaoman", "buding"})
        self.assertEqual(len(self.book.routines), 8)

    def test_same_phrase_routes_by_persona(self):
        self.assertEqual(
            self.book.match("phrase", "  我回来了！ ", "xiaoman"),
            "xiaoman_owner_returns",
        )
        self.assertEqual(
            self.book.match("phrase", "我回来了", "buding"),
            "buding_owner_returns",
        )
        self.assertIsNone(self.book.match("phrase", "我回来了"))

    def test_match_all_exposes_both_persona_variants(self):
        self.assertEqual(
            {item["persona"] for item in self.book.match_all("phrase", "我今天有点累")},
            {"xiaoman", "buding"},
        )

    def test_unknown_trigger_never_selects_a_routine(self):
        self.assertIsNone(self.book.match("phrase", "随便走两步", "xiaoman"))
        self.assertIsNone(self.book.match("button", "unknown", "buding"))

    def test_low_battery_blocks_without_dispatching_steps(self):
        result = ChoreographyRunner(self.book).run(
            "xiaoman_owner_returns",
            {**self.ready, "battery": 13},
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["steps"], [])
        self.assertIn("battery_below_minimum_or_unknown", result["reasons"])

    def test_ready_dry_run_preserves_xiaoman_step_order(self):
        result = ChoreographyRunner(self.book).run("xiaoman_owner_returns", self.ready)
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["persona"], "xiaoman")
        self.assertEqual(
            [step["step_id"] for step in result["steps"]],
            [
                "sample_sound",
                "confirm_footsteps",
                "welcome_line",
                "small_happy",
                "low_warm_light",
                "reset_head",
            ],
        )

    def test_persona_contrast_is_encoded_in_actions(self):
        xiaoman = self.book.get("xiaoman_owner_returns")
        buding = self.book.get("buding_owner_returns")
        xiaoman_dags = {step["args"]["task_id"] for step in xiaoman["steps"] if step["action"] == "native_dag"}
        buding_dags = {step["args"]["task_id"] for step in buding["steps"] if step["action"] == "native_dag"}
        self.assertEqual(xiaoman_dags, set())
        self.assertEqual(buding_dags, {"head_basic_up", "HAPPY_BOUNCE", "HIGH_FIVE_L"})
        self.assertLess(
            self.book.personas["xiaoman"]["max_intensity"],
            self.book.personas["buding"]["max_intensity"],
        )

    def test_meeting_requires_peer_presence(self):
        result = ChoreographyRunner(self.book).run("xiaoman_meets_buding", self.ready)
        self.assertIn("peer_not_present", result["reasons"])
        allowed = ChoreographyRunner(self.book).run(
            "xiaoman_meets_buding",
            {**self.ready, "peer_present": True},
        )
        self.assertEqual(allowed["status"], "simulated")

    def test_operator_stationary_and_fault_state_are_checked(self):
        for fields, reason in [
            ({"operator_present": False}, "operator_not_present"),
            ({"stationary": False}, "robot_not_confirmed_stationary"),
            ({"fault": True}, "fault_or_unknown"),
        ]:
            with self.subTest(fields=fields):
                result = ChoreographyRunner(self.book).run(
                    "xiaoman_show_identity",
                    {**self.ready, **fields},
                )
                self.assertIn(reason, result["reasons"])

    def test_validation_rejects_unbounded_head_angle(self):
        payload = copy.deepcopy(self.book.payload)
        payload["routines"][0]["steps"][1]["args"]["yaw"] = 0.5
        with self.assertRaisesRegex(ValidationError, "head angle"):
            RoutineBook(payload)

    def test_native_dag_must_remain_not_run(self):
        payload = copy.deepcopy(self.book.payload)
        target = next(
            step
            for routine in payload["routines"]
            for step in routine["steps"]
            if step["action"] == "native_dag"
        )
        target["verification"] = "service_only"
        with self.assertRaisesRegex(ValidationError, "must remain not_run"):
            RoutineBook(payload)

    def test_native_dag_ids_match_capability_snapshot(self):
        capabilities = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
        known = {
            (item["file"], item["task_id"])
            for item in capabilities["native_actions"]
        }
        configured = {
            (step["args"]["source_file"], step["args"]["task_id"])
            for routine in self.book.routines.values()
            for step in routine["steps"]
            if step["action"] == "native_dag"
        }
        self.assertLessEqual(configured, known)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the deterministic native Agent action policy."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from native_agent.policy import (
    ActionIntent,
    DEFAULT_ALLOWLIST_PATH,
    PolicyConfigError,
    RobotState,
    RunMode,
    ToolRisk,
    classify_tool,
    evaluate_action,
    issue_action_approval,
    load_action_allowlist,
)


NOW = 1_000.0
APPROVAL_SECRET = b"native-agent-policy-test-key-32b"
APPROVAL_NONCE = "approval_nonce_0001"


class ActionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_action_allowlist()

    def setUp(self) -> None:
        self.healthy = RobotState(
            battery_percent=80.0,
            charging=False,
            fault=False,
            stationary=True,
            sensors_observed_at_s=NOW - 0.1,
            person_present=True,
            action_busy=False,
            last_physical_action_at_s=None,
            physical_action_history_known=True,
        )

    def decide(
        self,
        tool: str,
        mode: RunMode = RunMode.SUPERVISED_DEMO,
        *,
        state: RobotState | None = None,
        approved: bool = True,
        now_s: float = NOW,
        arguments: dict[str, object] | None = None,
        consumed_nonces: frozenset[str] | None = frozenset(),
    ):
        intent = ActionIntent(tool=tool, mode=mode, arguments=arguments or {})
        tool_policy = self.policy.tool_policy(tool)
        risk_policy = None if tool_policy is None else self.policy.risk_policy(tool_policy.risk)
        if approved and risk_policy is not None and mode in risk_policy.supervisor_approval_modes:
            intent = replace(
                intent,
                approval=issue_action_approval(
                    intent,
                    approved_by="operator:test",
                    issued_at_s=NOW - 1,
                    expires_at_s=NOW + 30,
                    nonce=APPROVAL_NONCE,
                    secret=APPROVAL_SECRET,
                ),
            )
        return evaluate_action(
            intent,
            self.healthy if state is None else state,
            self.policy,
            now_s=now_s,
            approval_secret=APPROVAL_SECRET,
            consumed_approval_nonces=consumed_nonces,
        )

    def test_checked_in_allowlist_classifies_all_native_agent_tools(self) -> None:
        expected = {
            "activate_skill",
            "body_move",
            "body_turn",
            "cancel_schedule",
            "celebration",
            "dance_or_spin",
            "end_turn",
            "glance",
            "handshake_or_high_five",
            "head_move",
            "interact",
            "jump",
            "list_schedules",
            "low_power_mode",
            "mailbox_poll",
            "mailbox_wait",
            "memory_manage",
            "meta_action",
            "move_mode",
            "nav_to",
            "pose",
            "query_battery",
            "set_schedule",
            "speed_control",
            "spotlight",
            "take_photo",
            "volume_control",
            "web_search",
        }
        self.assertEqual({tool.tool for tool in self.policy.tools}, expected)
        self.assertEqual(classify_tool("head_move", self.policy), ToolRisk.EXPRESSIVE)
        self.assertEqual(classify_tool("body_move", self.policy), ToolRisk.BODY_MOTION)
        self.assertEqual(classify_tool("nav_to", self.policy), ToolRisk.NAVIGATION)
        self.assertEqual(classify_tool("jump", self.policy), ToolRisk.JUMP)
        self.assertEqual(classify_tool("take_photo", self.policy), ToolRisk.PRIVACY_CAPTURE)
        self.assertEqual(classify_tool("web_search", self.policy), ToolRisk.NETWORK_EGRESS)

    def test_allowlist_loader_rejects_duplicate_json_keys(self) -> None:
        original = Path(DEFAULT_ALLOWLIST_PATH).read_text(encoding="utf-8")
        duplicated = original.replace(
            '"schema_version": 1,',
            '"schema_version": 1, "schema_version": 1,',
            1,
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "allowlist.json"
            path.write_text(duplicated, encoding="utf-8")
            with self.assertRaisesRegex(PolicyConfigError, "duplicate_json_key"):
                load_action_allowlist(path)

    def test_read_only_tool_remains_available_for_diagnostics(self) -> None:
        unsafe = RobotState(
            battery_percent=1.0,
            charging=True,
            fault=True,
            stationary=False,
            sensors_observed_at_s=None,
            person_present=False,
            action_busy=True,
        )
        decision = self.decide(
            "query_battery",
            RunMode.IDENTITY_ONLY,
            state=unsafe,
            approved=False,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason_codes, ("allowed",))

    def test_memory_and_schedule_writes_require_explicit_approval(self) -> None:
        for tool in ("memory_manage", "set_schedule", "cancel_schedule"):
            with self.subTest(tool=tool):
                denied = self.decide(
                    tool,
                    RunMode.IDENTITY_ONLY,
                    approved=False,
                )
                self.assertFalse(denied.allowed)
                self.assertEqual(
                    denied.reason_codes,
                    ("action_approval_required",),
                )
                self.assertTrue(
                    self.decide(tool, RunMode.IDENTITY_ONLY, approved=True).allowed
                )

    def test_identity_only_rejects_every_physical_class(self) -> None:
        for tool in ("head_move", "body_move", "nav_to", "jump"):
            with self.subTest(tool=tool):
                decision = self.decide(tool, RunMode.IDENTITY_ONLY)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason_codes, ("mode_not_allowed",))

    def test_supervised_physical_action_requires_explicit_approval(self) -> None:
        decision = self.decide("head_move", approved=False)
        self.assertFalse(decision.allowed)
        self.assertIn("action_approval_required", decision.reason_codes)

    def test_healthy_supervised_expressive_action_is_allowed(self) -> None:
        decision = self.decide("head_move")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.risk, ToolRisk.EXPRESSIVE)
        self.assertEqual(decision.reason_code, "allowed")

    def test_autonomous_safe_keeps_physical_expression_disabled_until_parameter_gate(self) -> None:
        decision = self.decide(
            "spotlight",
            RunMode.AUTONOMOUS_SAFE,
            approved=False,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_codes, ("mode_not_allowed",))

    def test_autonomous_safe_rejects_body_navigation_and_jump(self) -> None:
        for tool in ("body_turn", "nav_to", "jump", "meta_action"):
            with self.subTest(tool=tool):
                decision = self.decide(
                    tool,
                    RunMode.AUTONOMOUS_SAFE,
                    approved=False,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason_codes, ("mode_not_allowed",))

    def test_each_robot_state_gate_rejects_an_expressive_action(self) -> None:
        cases = (
            (replace(self.healthy, battery_percent=24.9), "battery_below_minimum"),
            (replace(self.healthy, charging=True), "robot_charging"),
            (replace(self.healthy, fault=True), "robot_fault"),
            (replace(self.healthy, stationary=False), "robot_not_stationary"),
            (replace(self.healthy, sensors_observed_at_s=NOW - 2.01), "sensors_stale"),
            (replace(self.healthy, person_present=False), "person_not_present"),
            (replace(self.healthy, action_busy=True), "action_busy"),
            (
                replace(self.healthy, last_physical_action_at_s=NOW - 2.99),
                "cooldown_active",
            ),
        )
        for state, reason in cases:
            with self.subTest(reason=reason):
                decision = self.decide("head_move", state=state)
                self.assertFalse(decision.allowed)
                self.assertIn(reason, decision.reason_codes)

    def test_unknown_required_state_fails_closed(self) -> None:
        decision = self.decide("head_move", state=RobotState())
        self.assertFalse(decision.allowed)
        self.assertEqual(
            set(decision.reason_codes),
            {
                "fault_state_unknown",
                "action_busy_state_unknown",
                "charging_state_unknown",
                "battery_state_unknown",
                "stationary_state_unknown",
                "sensor_timestamp_unknown",
                "person_presence_unknown",
                "cooldown_history_unknown",
            },
        )

    def test_nav_jump_and_body_motion_have_progressively_stricter_defaults(self) -> None:
        expression = self.policy.risk_policy(ToolRisk.EXPRESSIVE)
        body = self.policy.risk_policy(ToolRisk.BODY_MOTION)
        navigation = self.policy.risk_policy(ToolRisk.NAVIGATION)
        jump = self.policy.risk_policy(ToolRisk.JUMP)
        self.assertIsNotNone(expression)
        self.assertIsNotNone(body)
        self.assertIsNotNone(navigation)
        self.assertIsNotNone(jump)
        assert expression is not None and body is not None
        assert navigation is not None and jump is not None
        self.assertLess(expression.minimum_battery_percent, body.minimum_battery_percent)
        self.assertLess(body.minimum_battery_percent, navigation.minimum_battery_percent)
        self.assertLess(navigation.minimum_battery_percent, jump.minimum_battery_percent)
        self.assertGreater(expression.max_sensor_age_seconds, body.max_sensor_age_seconds)
        self.assertGreater(body.max_sensor_age_seconds, navigation.max_sensor_age_seconds)
        self.assertGreater(navigation.max_sensor_age_seconds, jump.max_sensor_age_seconds)
        self.assertLess(expression.cooldown_seconds, body.cooldown_seconds)
        self.assertLess(body.cooldown_seconds, navigation.cooldown_seconds)
        self.assertLess(navigation.cooldown_seconds, jump.cooldown_seconds)

    def test_stricter_battery_thresholds_are_enforced(self) -> None:
        cases = (
            ("head_move", 25.0, True),
            ("body_move", 39.9, False),
            ("body_move", 40.0, True),
            ("nav_to", 49.9, False),
            ("nav_to", 50.0, True),
            ("jump", 59.9, False),
            ("jump", 60.0, True),
        )
        for tool, battery, allowed in cases:
            with self.subTest(tool=tool, battery=battery):
                decision = self.decide(
                    tool,
                    state=replace(self.healthy, battery_percent=battery),
                )
                self.assertEqual(decision.allowed, allowed)

    def test_sensor_and_cooldown_boundaries_are_inclusive(self) -> None:
        at_boundary = replace(
            self.healthy,
            sensors_observed_at_s=NOW - 2.0,
            last_physical_action_at_s=NOW - 3.0,
        )
        self.assertTrue(self.decide("head_move", state=at_boundary).allowed)

        future_sensor = replace(self.healthy, sensors_observed_at_s=NOW + 0.01)
        decision = self.decide("head_move", state=future_sensor)
        self.assertFalse(decision.allowed)
        self.assertIn("sensor_timestamp_in_future", decision.reason_codes)

    def test_unknown_tool_and_mode_are_denied(self) -> None:
        unknown_tool = self.decide("execute_x5_command")
        self.assertFalse(unknown_tool.allowed)
        self.assertEqual(unknown_tool.reason_codes, ("tool_not_allowlisted",))

        invalid_mode = evaluate_action(
            ActionIntent(tool="query_battery", mode="anything-goes"),
            self.healthy,
            self.policy,
            now_s=NOW,
        )
        self.assertFalse(invalid_mode.allowed)
        self.assertEqual(invalid_mode.reason_codes, ("invalid_mode",))

    def test_policy_evaluation_is_deterministic_and_does_not_mutate_inputs(self) -> None:
        intent = ActionIntent(
            tool="head_move",
            mode=RunMode.SUPERVISED_DEMO,
            arguments={"pitch_deg": 5},
        )
        intent = replace(
            intent,
            approval=issue_action_approval(
                intent,
                approved_by="operator:test",
                issued_at_s=NOW - 1,
                expires_at_s=NOW + 30,
                nonce=APPROVAL_NONCE,
                secret=APPROVAL_SECRET,
            ),
        )
        before = self.healthy
        first = evaluate_action(
            intent,
            self.healthy,
            self.policy,
            now_s=NOW,
            approval_secret=APPROVAL_SECRET,
            consumed_approval_nonces=frozenset(),
        )
        second = evaluate_action(
            intent,
            self.healthy,
            self.policy,
            now_s=NOW,
            approval_secret=APPROVAL_SECRET,
            consumed_approval_nonces=frozenset(),
        )
        self.assertEqual(first, second)
        self.assertEqual(self.healthy, before)

    def test_approval_is_bound_to_exact_arguments_and_replay_state(self) -> None:
        original = ActionIntent(
            tool="head_move",
            mode=RunMode.SUPERVISED_DEMO,
            arguments={"pitch_deg": 5, "yaw_deg": 0},
        )
        approval = issue_action_approval(
            original,
            approved_by="operator:test",
            issued_at_s=NOW - 1,
            expires_at_s=NOW + 30,
            nonce=APPROVAL_NONCE,
            secret=APPROVAL_SECRET,
        )
        changed = replace(
            original,
            arguments={"pitch_deg": 45, "yaw_deg": 0},
            approval=approval,
        )
        mismatch = evaluate_action(
            changed,
            self.healthy,
            self.policy,
            now_s=NOW,
            approval_secret=APPROVAL_SECRET,
            consumed_approval_nonces=frozenset(),
        )
        self.assertFalse(mismatch.allowed)
        self.assertIn("approval_intent_mismatch", mismatch.reason_codes)

        replay = evaluate_action(
            replace(original, approval=approval),
            self.healthy,
            self.policy,
            now_s=NOW,
            approval_secret=APPROVAL_SECRET,
            consumed_approval_nonces=frozenset({APPROVAL_NONCE}),
        )
        self.assertFalse(replay.allowed)
        self.assertIn("approval_replayed", replay.reason_codes)

    def test_approval_requires_verifier_replay_store_and_valid_time(self) -> None:
        intent = ActionIntent(tool="set_schedule", mode=RunMode.IDENTITY_ONLY)
        approval = issue_action_approval(
            intent,
            approved_by="operator:test",
            issued_at_s=NOW - 20,
            expires_at_s=NOW - 10,
            nonce=APPROVAL_NONCE,
            secret=APPROVAL_SECRET,
        )
        decision = evaluate_action(
            replace(intent, approval=approval),
            self.healthy,
            self.policy,
            now_s=NOW,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("approval_verifier_unavailable", decision.reason_codes)
        self.assertIn("approval_replay_store_unavailable", decision.reason_codes)
        self.assertIn("approval_expired", decision.reason_codes)

    def test_approval_signature_cannot_be_forged(self) -> None:
        intent = ActionIntent(tool="volume_control", mode=RunMode.SUPERVISED_DEMO)
        approval = issue_action_approval(
            intent,
            approved_by="operator:test",
            issued_at_s=NOW - 1,
            expires_at_s=NOW + 30,
            nonce=APPROVAL_NONCE,
            secret=APPROVAL_SECRET,
        )
        forged = replace(approval, approved_by="operator:other")
        decision = evaluate_action(
            replace(intent, approval=forged),
            self.healthy,
            self.policy,
            now_s=NOW,
            approval_secret=APPROVAL_SECRET,
            consumed_approval_nonces=frozenset(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("approval_signature_invalid", decision.reason_codes)

    def test_invalid_argument_shape_fails_closed(self) -> None:
        decision = evaluate_action(
            ActionIntent(
                tool="query_battery",
                mode=RunMode.IDENTITY_ONLY,
                arguments={"Bad-Key": object()},
            ),
            self.healthy,
            self.policy,
            now_s=NOW,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_codes, ("invalid_arguments",))

    def test_photo_and_web_search_require_signed_approval(self) -> None:
        photo = self.decide("take_photo", approved=False)
        search = self.decide(
            "web_search", RunMode.IDENTITY_ONLY, approved=False
        )
        self.assertEqual(photo.reason_codes, ("action_approval_required",))
        self.assertEqual(search.reason_codes, ("action_approval_required",))


if __name__ == "__main__":
    unittest.main()

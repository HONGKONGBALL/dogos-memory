import threading
import unittest
from pathlib import Path

from demo_choreo import ChoreographyRunner, ExecutionCancelled, RoutineBook
from demo_choreo.live import LiveDriver, LiveStateProbe


BOOK = Path(__file__).resolve().parents[1] / "routines.json"


class FakeClient:
    def __init__(self):
        self.calls = []

    def head(self, pitch, yaw):
        self.calls.append(("head", pitch, yaw))
        return {"status": "service_acknowledged"}

    def light(self, rgb, **kwargs):
        self.calls.append(("light", rgb, kwargs))
        return {"status": "service_acknowledged"}

    def emotion(self, mode, **kwargs):
        self.calls.append(("emotion", mode, kwargs))
        return {"status": "service_acknowledged"}

    def say(self, text):
        self.calls.append(("say", text))
        return {"status": "service_acknowledged"}

    def stop_expression(self, mode):
        self.calls.append(("stop_expression", mode))
        return [{"status": "service_acknowledged"}]


class FakeSensors:
    def __init__(self, observation):
        self.value = observation
        self.started = False
        self.stop = threading.Event()

    def start(self):
        self.started = True

    def observation(self, max_age=5):
        return self.value

    def status(self):
        return {"source": "fake"}


class LiveRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.driver = LiveDriver(
            client=self.client,
            resources={"emotions": {"happy": 1}},
            wait=lambda _seconds: False,
        )

    def test_expression_steps_map_to_bounded_client_calls(self):
        self.driver.execute({
            "id": "light",
            "action": "light",
            "args": {"rgb": [1, 2, 3], "brightness": 12, "duration_ms": 500},
        })
        self.driver.execute({
            "id": "emotion",
            "action": "emotion",
            "args": {"candidates": ["missing", "happy"], "duration_ms": 600},
        })
        self.assertIn(
            ("light", (1, 2, 3), {"brightness": 12, "duration_ms": 500}),
            self.client.calls,
        )
        self.assertIn(("emotion", 1, {"duration_ms": 600}), self.client.calls)

    def test_native_dag_is_skipped_without_dispatch(self):
        result = self.driver.execute({
            "id": "jump",
            "action": "native_dag",
            "args": {"task_id": "HAPPY_BOUNCE", "source_file": "HAPPY_JUMP.json"},
        })
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.client.calls, [])

    def test_per_step_guard_blocks_stale_or_charging_state(self):
        driver = LiveDriver(
            client=self.client,
            resources={"emotions": {}},
            wait=lambda _seconds: False,
            guard=lambda: {
                "battery": 80,
                "charging": True,
                "stationary": True,
                "operator_present": True,
                "fault": False,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "charging_or_unknown"):
            driver.execute({
                "id": "line",
                "action": "say",
                "args": {"text": "hello"},
            })
        self.assertEqual(self.client.calls, [])

    def test_per_step_guard_keeps_routine_battery_threshold(self):
        driver = LiveDriver(
            client=self.client,
            resources={"emotions": {}},
            wait=lambda _seconds: False,
            guard=lambda: {
                "battery": 39,
                "charging": False,
                "stationary": True,
                "operator_present": True,
                "fault": False,
            },
        )
        driver.begin_routine(1000, min_battery=40)
        with self.assertRaisesRegex(RuntimeError, "battery_below_40_or_unknown"):
            driver.execute({
                "id": "line",
                "action": "say",
                "args": {"text": "hello"},
            })
        self.assertEqual(self.client.calls, [])

    def test_routine_deadline_stops_pacing(self):
        moments = iter((10.0, 10.0, 10.2))
        driver = LiveDriver(
            client=self.client,
            resources={"emotions": {}},
            wait=lambda _seconds: False,
            clock=lambda: next(moments),
        )
        driver.begin_routine(100)
        with self.assertRaisesRegex(RuntimeError, "routine_timeout"):
            driver.execute({
                "id": "pause",
                "action": "wait",
                "args": {"duration_ms": 500},
            })

    def test_operator_stop_cancels_the_next_step_and_requests_cleanup(self):
        result = self.driver.request_stop()
        self.assertTrue(result)
        with self.assertRaises(ExecutionCancelled):
            self.driver.execute({
                "id": "line",
                "action": "say",
                "args": {"text": "hello"},
            })
        self.assertIn(("stop_expression", None), self.client.calls)

    def test_runner_reports_completed_with_skips(self):
        book = RoutineBook.load(BOOK)
        result = ChoreographyRunner(book, self.driver).run(
            "buding_show_identity",
            {
                "battery": 80,
                "charging": False,
                "stationary": True,
                "operator_present": True,
                "peer_present": False,
                "fault": False,
            },
        )
        self.assertEqual(result["status"], "completed_with_skips")
        self.assertTrue(any(step["result"]["status"] == "skipped" for step in result["steps"]))

    def test_live_probe_fails_closed_then_uses_fresh_telemetry(self):
        missing = LiveStateProbe(sensors=FakeSensors(None))
        self.assertFalse(missing.wait_ready(timeout_seconds=0.01))
        self.assertTrue(missing.context(operator_present=True, peer_present=False)["fault"])

        ready = LiveStateProbe(sensors=FakeSensors({
            "battery": 73,
            "charging": False,
            "busy": False,
            "present": False,
        }))
        self.assertTrue(ready.wait_ready(timeout_seconds=0.01))
        self.assertEqual(
            ready.context(operator_present=True, peer_present=True),
            {
                "battery": 73,
                "charging": False,
                "stationary": True,
                "operator_present": True,
                "peer_present": True,
                "fault": False,
            },
        )


if __name__ == "__main__":
    unittest.main()

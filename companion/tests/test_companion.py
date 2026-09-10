import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Companion, Config
from adapter import Adapter


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.dog = Companion(Config(observation_ttl=1000, greeting_enabled=False))
        self.scene = dict(present=True, busy=False, charging=False, battery=80)
        self.dog.event('observation', 0, self.scene)

    def test_complete_unprompted_invite_ignore_backoff(self):
        self.assertEqual(self.dog.event('start', 0), ['invite'])
        for t in range(1, 12):
            self.assertEqual(self.dog.tick(t), [])
        self.assertEqual(self.dog.tick(12), [])
        self.assertEqual(self.dog.state, 'quiet')
        self.assertEqual(self.dog.ignored, 1)
        self.assertEqual(self.dog.tick(131), [])
        self.assertEqual(self.dog.tick(132), ['invite'])
        self.dog.tick(144)
        self.assertEqual(self.dog.next_invitation, 384)

    def test_response_returns_to_quiet_and_persists_counts_only(self):
        self.dog.event('start', 0)
        self.assertEqual(self.dog.event('respond', 3), ['acknowledge'])
        self.assertEqual(self.dog.event('respond', 4), [])
        self.dog.tick(13)
        self.assertEqual(self.dog.state, 'quiet')
        restored = Companion(memory=self.dog.memory())
        self.assertEqual(restored.interactions, 1)
        self.assertEqual(restored.state, 'paused')
        self.assertFalse(restored.present)

    def test_busy_charging_absence_low_battery_cancel_invitation(self):
        for field, value in [('busy', True), ('charging', True), ('present', False), ('battery', 20)]:
            with self.subTest(field=field):
                dog = Companion(Config(greeting_enabled=False))
                dog.event('observation', 0, self.scene)
                dog.event('start', 0)
                dog.event('observation', 1, {**self.scene, field: value})
                self.assertEqual(dog.state, 'resting' if field in ('charging','battery') else 'quiet')
                self.assertEqual(dog.event('respond', 2), [])

    def test_stale_observations_and_late_responses_do_not_act(self):
        dog = Companion(Config(greeting_enabled=False))
        dog.event('observation', 0, self.scene)
        dog.event('start', 0)
        self.assertEqual(dog.event('respond', 12), [])
        self.assertEqual(dog.interactions, 0)
        self.assertEqual(dog.tick(300), [])
        self.assertIn('过期', dog.reason)

    def test_pause_and_fault_are_latched(self):
        self.dog.event('start', 0)
        self.dog.event('pause', 1)
        self.assertEqual(self.dog.event('respond', 2), [])
        self.assertEqual(self.dog.tick(200), [])
        self.dog.event('failure', 201)
        self.assertEqual(self.dog.event('start', 202), [])
        self.assertEqual(self.dog.state, 'fault')
        self.dog.event('reset_fault', 203)
        self.assertEqual(self.dog.state, 'paused')

    def test_duplicate_start_does_not_duplicate_invitation(self):
        self.assertEqual(self.dog.event('start', 0), ['invite'])
        self.assertEqual(self.dog.event('start', 1), [])
        self.assertEqual(self.dog.state, 'inviting')


class AdapterTests(unittest.TestCase):
    @patch('adapter.subprocess.run')
    def test_simulation_never_calls_hardware(self, run):
        self.assertEqual(Adapter().execute('invite')['status'], 'simulated')
        run.assert_not_called()

    @patch('adapter.subprocess.run')
    def test_transport_success_is_not_native_success(self, run):
        for value in ({'success': True}, {'success': True, 'response': {'success': False}}):
            run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({'content': [{'type': 'text', 'text': json.dumps(value)}]}))
            with self.assertRaises(RuntimeError):
                Adapter(live=True).execute('invite')

    @patch('adapter.subprocess.run')
    def test_native_ack_still_does_not_claim_physical_effect(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({'content': [{'type': 'text', 'text': json.dumps({'success': True, 'response': {'success': True}})}]}))
        result = Adapter(live=True).execute('acknowledge')
        self.assertEqual(result['physical_effect'], 'unverified')
        self.assertEqual(run.call_args.args[0][2], 'datou_light')


if __name__ == '__main__':
    unittest.main()

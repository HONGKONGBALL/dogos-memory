import json
import threading
import unittest
from pathlib import Path

from websockets.sync.server import serve

from companion.native import NativeClient
from companion.sensors import Sensors
from demo_choreo import ChoreographyRunner, RoutineBook
from demo_choreo.live import LiveDriver, LiveStateProbe


BOOK = Path(__file__).resolve().parents[1] / "routines.json"


class LocalBridge:
    """Small rosbridge-protocol stand-in; it never touches robot hardware."""

    def __init__(self):
        self.calls = []
        self.server = serve(self.handle, "127.0.0.1", 0)
        port = self.server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=2)

    def handle(self, websocket):
        for raw in websocket:
            request = json.loads(raw)
            if request["op"] == "subscribe":
                topic = request["topic"]
                if topic == "/bms_state":
                    self.publish(websocket, topic, {
                        "soc_percent": 83,
                        "is_charger_connected": False,
                        "request_shutdown": False,
                        "alarm": 0,
                    })
                elif topic == "/function/context/context_snapshot":
                    self.publish(websocket, topic, {
                        "dag_status": {"emergency_stop_active": False},
                        "system_status": {
                            "joy_override_active": False,
                            "robot_is_static": True,
                            "agent_enable": False,
                        },
                        "function_status": {
                            "follow_status": {"status": 0},
                            "nav_status": {"status": 0},
                        },
                    })
                continue

            if request["op"] == "call_service":
                service = request["service"]
                if service == "/datou/resources":
                    values = {"emotions": {"happy": 7}, "sounds": []}
                else:
                    self.calls.append((service, request.get("args", {})))
                    values = {"success": True}
                websocket.send(json.dumps({
                    "op": "service_response",
                    "id": request["id"],
                    "service": service,
                    "result": True,
                    "values": values,
                }))

    @staticmethod
    def publish(websocket, topic, message):
        websocket.send(json.dumps({"op": "publish", "topic": topic, "msg": message}))


class LiveTransportTests(unittest.TestCase):
    def test_real_websocket_transport_runs_one_bounded_routine(self):
        bridge = LocalBridge()
        bridge.start()
        sensors = Sensors(url=bridge.url)
        probe = LiveStateProbe(url=bridge.url, sensors=sensors)
        try:
            self.assertTrue(probe.wait_ready(timeout_seconds=2))
            context = probe.context(operator_present=True, peer_present=False)
            client = NativeClient(url=bridge.url)
            driver = LiveDriver(
                url=bridge.url,
                client=client,
                guard=lambda: probe.context(operator_present=True, peer_present=False),
                wait=lambda _seconds: False,
            )
            routine = RoutineBook.load(BOOK).get("xiaoman_show_identity")
            driver.begin_routine(
                routine["max_duration_ms"],
                min_battery=routine["preconditions"]["min_battery"],
            )
            result = ChoreographyRunner(RoutineBook.load(BOOK), driver).run(
                routine["id"],
                context,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                {service for service, _args in bridge.calls},
                {"/head_action", "/set_speak", "/light_node/control"},
            )
        finally:
            probe.close()
            bridge.close()


if __name__ == "__main__":
    unittest.main()

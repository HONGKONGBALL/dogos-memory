"""Real telemetry cache. Never substitutes manual presence or stale samples."""
import asyncio
import json
import math
import threading
import time
from collections import deque

GROUPS = [
    ['/bms_state', '/function/context/context_snapshot', '/perception/detections2d', '/perception/poses'],
    ['/touch_node/touch_state', '/touch_node/touch_event', '/uwb/state', '/slam/status'],
]


class Sensors:
    def __init__(self, url='ws://127.0.0.1:19091', clock=time.monotonic):
        self.url, self.clock = url, clock
        self.lock = threading.RLock()
        self.samples = {}
        self.stamps = {}
        self.edges = deque(maxlen=16)
        self.pressed = {}
        self.last_touch = float('-inf')
        self.last_person = float('-inf')
        self.errors = {}
        self.stop = threading.Event()

    def ingest(self, topic, msg):
        now = self.clock()
        with self.lock:
            # Repeated publisher timestamps cannot keep a frozen camera fresh.
            stamp = msg.get('header', {}).get('stamp') or msg.get('timestamp')
            if stamp:
                key = json.dumps(stamp, sort_keys=True)
                if self.stamps.get(topic) == key:
                    return
                self.stamps[topic] = key
            self.samples[topic] = (now, msg)
            if topic == '/perception/poses' and msg.get('class_id') == 0 and self.score(msg.get('score')):
                self.last_person = now
            if topic == '/perception/detections2d':
                for detection in msg.get('detections', []):
                    for result in detection.get('results', []):
                        h = result.get('hypothesis', {})
                        # Numeric class IDs require model metadata; do not assume COCO.
                        if str(h.get('class_id', '')).lower() in ('person', 'human') and self.score(h.get('score')):
                            self.last_person = now
            if topic.startswith('/touch_node/touch_'):
                idx = (msg.get('source'), msg.get('idx'))
                previous = self.pressed.get(idx)
                state = msg.get('state')
                if topic.endswith('touch_state'):
                    self.pressed[idx] = state == 1
                    triggered = previous is False and state == 1
                else:
                    triggered = state in (10, 11, 12)
                if triggered and now - self.last_touch > 2:
                    self.last_touch = now
                    self.edges.append(now)

    @staticmethod
    def score(value):
        return type(value) in (int, float) and math.isfinite(value) and .65 <= value <= 1

    def observation(self, max_age=5):
        now = self.clock()
        with self.lock:
            def fresh(topic, ttl=max_age):
                item = self.samples.get(topic)
                return item[1] if item and now - item[0] < ttl else None
            bms = fresh('/bms_state')
            context = fresh('/function/context/context_snapshot')
            if bms is None or context is None:
                return None
            if 'snapshot' in context:
                context = context['snapshot']
            try:
                soc = bms['soc_percent']
                charging = bms['is_charger_connected']
                if type(soc) not in (int, float) or not math.isfinite(soc) or not 0 <= soc <= 100 or type(charging) is not bool:
                    return None
                sys = context['system_status']
                fs = context['function_status']
                blocked = (context['dag_status']['emergency_stop_active'] or sys['joy_override_active']
                    or not sys['robot_is_static'] or sys.get('agent_enable', False)
                    or fs['follow_status']['status'] != 0 or fs['nav_status']['status'] != 0
                    or bms.get('request_shutdown', True) or bms.get('alarm', 1) != 0)
            except KeyError:
                return None
            return dict(present=now - max(self.last_person, self.last_touch) < 8,
                        busy=bool(blocked), charging=charging, battery=soc)

    def take_touch(self):
        with self.lock:
            now = self.clock()
            valid = any(now - stamp < 2 for stamp in self.edges)
            self.edges.clear()
            return valid

    def status(self):
        with self.lock:
            uwb = self.samples.get('/uwb/state')
            slam = self.samples.get('/slam/status')
            return dict(source='robot_topics', topics={t: {'age_seconds': round(self.clock()-v[0], 1)}
                for t, v in self.samples.items()}, errors=dict(self.errors),
                person_detected=self.clock()-self.last_person < 8,
                touch_recent=self.clock()-self.last_touch < 8,
                follow_readiness={'tag_state':uwb[1].get('state') if uwb and self.clock()-uwb[0]<5 else None,
                    'execution_enabled':False,'remaining':'Tag ranging, physical follow and robot-side disconnect stop validation'},
                navigation_readiness={'map':slam[1].get('current_map_name') if slam and self.clock()-slam[0]<5 else None,
                    'execution_enabled':False,'remaining':'Known target, localization, cancellation and physical arrival validation'})

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True).start()

    async def run(self):
        await asyncio.gather(*(self.consume(group) for group in GROUPS))

    async def consume(self, topics):
        import websockets
        while not self.stop.is_set():
            try:
                async with websockets.connect(self.url, open_timeout=3, close_timeout=1, max_size=4*1024*1024) as ws:
                    for topic in topics:
                        await ws.send(json.dumps(dict(op='subscribe', topic=topic, id=topic)))
                    while not self.stop.is_set():
                        try:
                            value = json.loads(await asyncio.wait_for(ws.recv(), 1))
                        except asyncio.TimeoutError:
                            continue
                        if value.get('op') == 'publish':
                            self.ingest(value['topic'], value['msg'])
                            with self.lock:
                                self.errors.pop(value['topic'], None)
                        elif value.get('level') == 'error':
                            with self.lock:
                                self.errors[value.get('id', topics[0])] = value.get('msg')
            except Exception as error:
                with self.lock:
                    for topic in topics:
                        self.errors[topic] = str(error)
                        self.samples.pop(topic, None)
                    self.last_person = self.last_touch = float('-inf')
                    self.pressed.clear()
                    self.edges.clear()
                await asyncio.sleep(2)

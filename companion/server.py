"""Local companion laboratory. Default mode cannot access the robot."""
import argparse
import json
import math
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adapter import Adapter, PetAdapter
from engine import Companion


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--live', action='store_true', help='Use real telemetry and bounded native interactions')
    parser.add_argument('--enable-head', action='store_true', help='Enable bounded head angles after pre-check')
    parser.add_argument('--enable-sound', action='store_true', help='Enable optional short sound; audibility unverified')
    parser.add_argument('--node', default='node')
    parser.add_argument('--memory', type=Path, default=Path(__file__).with_name('memory.json'))
    opts = parser.parse_args()
    try:
        memory = json.loads(opts.memory.read_text())
        if not isinstance(memory, dict):
            raise ValueError('memory must be an object')
        engine = Companion(memory=memory)
    except FileNotFoundError:
        engine = Companion()
    except (ValueError, TypeError, OverflowError):
        raise SystemExit('Invalid companion memory; preserve the file and select a new --memory path')
    sensors = None
    if opts.live:
        from native import NativeClient
        from sensors import Sensors
        client = NativeClient()
        if opts.enable_head:
            client.head(0.0, .08, pre_check=True)
        adapter = PetAdapter(client, opts.enable_head, opts.enable_sound)
        sensors = Sensors()
    else:
        adapter = Adapter()
    lock = threading.RLock()
    events, pending = deque(maxlen=100), deque(maxlen=4)
    stopped = threading.Event()
    epoch = 0
    last_memory = None

    def log(kind, detail):
        events.appendleft(dict(time=time.strftime('%H:%M:%S'), kind=kind, detail=detail))

    def persist():
        nonlocal last_memory
        value = json.dumps(engine.memory())
        if value != last_memory:
            opts.memory.parent.mkdir(parents=True, exist_ok=True)
            temp = opts.memory.with_suffix('.tmp')
            temp.write_text(value)
            os.replace(temp, opts.memory)
            last_memory = value

    def apply(kind, data=None):
        nonlocal epoch
        before = (engine.state, engine.reason)
        actions = engine.event(kind, time.monotonic(), data)
        if kind in ('pause', 'reset_fault', 'failure', 'preference', 'touch') or engine.blocked(time.monotonic()):
            epoch += 1
            pending.clear()
        for action in actions:
            pending.append((epoch, action))
        if before != (engine.state, engine.reason):
            log('state', {'state': engine.state, 'reason': engine.reason})
        persist()

    def loop():
        while not stopped.wait(.2):
            try:
                with lock:
                    if sensors:
                        observation = sensors.observation()
                        if observation is None:
                            engine.seen_at = float('-inf')
                        else:
                            apply('observation', observation)
                        if sensors.take_touch():
                            apply('touch')
                    apply('tick')
                    item = pending.popleft() if pending else None
                    if item and (item[0] != epoch or engine.state in ('paused', 'fault')):
                        item = None
                if item:
                    try:
                        def dispatch(call):
                            # Serialize accepted pause against each native dispatch.
                            # An already-dispatched bounded request completes before pause is acknowledged.
                            with lock:
                                if sensors:
                                    latest = sensors.observation()
                                    if latest is None or latest['busy'] or latest['charging'] or latest['battery'] <= 25 or not latest['present']:
                                        return None
                                if item[0] != epoch or engine.state in ('paused', 'fault') or engine.blocked(time.monotonic()):
                                    return None
                                return call()
                        result = adapter.execute(item[1], dispatch) if opts.live else adapter.execute(item[1])
                        with lock:
                            log('action', {'action': item[1], **result})
                    except Exception as error:
                        with lock:
                            log('error', str(error))
                            apply('failure')
            except Exception as error:
                with lock:
                    pending.clear()
                    engine.event('failure', time.monotonic())
                    log('error', str(error))

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, value):
            payload = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == '/api/state':
                with lock:
                    return self.reply(200, {**engine.snapshot(), 'mode': 'live-companion' if opts.live else 'simulation',
                        'observation_source': 'robot_topics' if sensors else 'manual', 'events': list(events),
                        'sensors': sensors.status() if sensors else None,
                        'outputs': {'head': opts.enable_head, 'sound': opts.enable_sound},
                        'fresh': time.monotonic() - engine.seen_at < engine.config.observation_ttl})
            if self.path == '/api/capabilities':
                return self.reply(200, json.loads(Path(__file__).with_name('capabilities.json').read_text()))
            if self.path != '/':
                return self.reply(404, {'error': 'Not found'})
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(Path(__file__).with_name('index.html').read_bytes())

        def do_POST(self):
            allowed = [f'127.0.0.1:{opts.port}', f'localhost:{opts.port}']
            if self.headers.get('Host') not in allowed:
                return self.reply(403, {'error': 'Host rejected'})
            if self.headers.get('Origin') and self.headers['Origin'] not in ['http://' + h for h in allowed]:
                return self.reply(403, {'error': 'Origin rejected'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.reply(415, {'error': 'JSON required'})
            if self.path != '/api/event':
                return self.reply(404, {'error': 'Not found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('Invalid length')
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError('Object required')
                kind = value.get('kind')
                if kind not in ('start', 'pause', 'reset_fault', 'observation', 'respond', 'touch', 'preference'):
                    raise ValueError('Unknown event')
                if opts.live and kind in ('observation', 'respond', 'touch'):
                    return self.reply(409, {'error': '实机模式只接受真实传感输入；模拟输入已禁用'})
                data = value.get('data', {})
                if not isinstance(data, dict):
                    raise ValueError('Event data must be an object')
                if kind == 'observation':
                    if not isinstance(data, dict) or any(type(data.get(k)) is not bool for k in ('present', 'busy', 'charging')):
                        raise ValueError('Boolean observation fields required')
                    battery = data.get('battery')
                    if type(battery) not in (int, float) or not math.isfinite(battery) or not 0 <= battery <= 100:
                        raise ValueError('Battery must be 0–100')
                with lock:
                    apply(kind, data)
                self.reply(200, {'accepted': True})
            except (ValueError, TypeError) as error:
                self.reply(400, {'error': str(error)})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', opts.port), Handler)
    if sensors:
        sensors.start()
    worker = threading.Thread(target=loop, daemon=True)
    worker.start()
    print(f'Companion: http://127.0.0.1:{opts.port}/ | {"LIVE LIGHTS" if opts.live else "SIMULATION"}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        if sensors:
            sensors.stop.set()
        with lock:
            pending.clear()
            engine.event('pause', time.monotonic())
        server.server_close()


if __name__ == '__main__':
    main()

"""Exercise only the running simulation. Refuses live mode."""
import json
import time
import urllib.request

BASE = 'http://127.0.0.1:8766'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def state():
    return json.load(opener.open(BASE + '/api/state', timeout=3))


def event(kind, data=None):
    request = urllib.request.Request(BASE + '/api/event',
        data=json.dumps(dict(kind=kind, data=data or {})).encode(),
        headers={'Content-Type': 'application/json'})
    return json.load(opener.open(request, timeout=3))


assert state()['mode'] == 'simulation', 'Refusing to test against real robot'
assert '陪伴实验室' in opener.open(BASE, timeout=3).read().decode()
try:
    event('observation', dict(present=True, busy=False, charging=False, battery=80))
    event('start')
    time.sleep(.5)
    assert state()['state'] == 'inviting'
    assert any(e['kind'] == 'action' and e['detail']['status'] == 'simulated' for e in state()['events'])
    event('respond')
    time.sleep(.5)
    assert state()['state'] == 'interacting'
    time.sleep(10)
    assert state()['state'] == 'quiet'
finally:
    event('pause')
assert state()['state'] == 'paused'
print('HTTP PASS: scene → autonomous invite → response → quiet → pause; simulation only')

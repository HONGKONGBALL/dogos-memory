"""Bounded native-expression adapter; never invokes locomotion or speech."""
import json
import subprocess
from pathlib import Path

CALLER = Path(__file__).resolve().parents[1] / 'scripts' / 'call-datou.mjs'

BEHAVIORS = {
    'greet': {'rgb':(90,150,200),'emotions':('say_hi','WINK'),'angles':(0.0,.04),'sound':False},
    'invite': {'rgb':(50,100,255),'emotions':('LISTEN','WINK'),'angles':(0.0,.08),'sound':True},
    'acknowledge': {'rgb':(50,200,100),'emotions':('HAPPY_10S','happy'),'angles':(-.06,0.0),'sound':True},
    'look_around': {'rgb':(70,90,100),'emotions':('WINK',),'angles':(0.0,-.05),'sound':False},
}


class Adapter:
    def __init__(self, live=False, node='node'):
        self.live, self.node = live, node

    def execute(self, action):
        # Colors are our chosen design, not claims about native emotion labels.
        red, green, blue = BEHAVIORS[action]['rgb']
        args = dict(red=red, green=green, blue=blue, duration_ms=1500)
        if not self.live:
            return {'status': 'simulated', 'tool': 'datou_light', 'arguments': args}
        result = subprocess.run([self.node, str(CALLER), 'datou_light', json.dumps(args)],
                                capture_output=True, text=True, timeout=25)
        if result.returncode:
            raise RuntimeError('原生接口调用失败，未确认实际效果')
        try:
            payload = json.loads(result.stdout)
            if payload.get('isError'):
                raise ValueError('MCP error')
            text = next(c['text'] for c in payload['content'] if c['type'] == 'text')
            value = json.loads(text)
            if value.get('success') is not True or value.get('response', {}).get('success') is not True:
                raise ValueError('native acknowledgement missing')
        except (ValueError, KeyError, StopIteration, TypeError):
            raise RuntimeError('返回格式或执行结果未确认') from None
        return {'status': 'service_acknowledged', 'physical_effect': 'unverified',
                'tool': 'datou_light', 'arguments': args}


class PetAdapter:
    """Execute one bounded native skill at a time through the runtime dispatch gate."""
    def __init__(self, client, head=False, sound=False):
        self.client, self.head_enabled, self.sound_enabled = client, head, sound
        self.resources = client.call('/datou/resources')

    def execute(self, action, dispatch):
        if action not in BEHAVIORS:
            raise ValueError('Unknown companion action')
        results = []
        profile = BEHAVIORS[action]
        rgb = profile['rgb']
        steps = [('light', lambda: self.client.light(rgb))]
        emotions = self.resources.get('emotions', {})
        name = next((n for n in profile['emotions'] if n in emotions), None)
        if name:
            steps.append(('emotion', lambda: self.client.emotion(emotions[name])))
        if self.head_enabled:
            steps.append(('head', lambda: self.client.head(*profile['angles'])))
        if self.sound_enabled and profile['sound'] and 'happy_short' in self.resources.get('sounds', []):
            steps.append(('sound', lambda: self.client.sound('happy_short')))
        for name, call in steps:
            result = dispatch(call)
            if result is None:
                return {'status': 'cancelled', 'steps': results}
            results.append({'skill': name, **result})
        return {'status': 'service_acknowledged', 'physical_effect': 'unverified', 'steps': results}

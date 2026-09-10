"""ROS bridge client with request correlation and explicit native acknowledgements."""
import json
import uuid


class NativeClient:
    def __init__(self, url='ws://127.0.0.1:19091'):
        self.url = url

    def call(self, service, args=None):
        from websockets.sync.client import connect
        request_id = uuid.uuid4().hex
        with connect(self.url, open_timeout=3, close_timeout=1, max_size=8 * 1024 * 1024) as ws:
            ws.send(json.dumps(dict(op='call_service', id=request_id, service=service, args=args or {})))
            while True:
                result = json.loads(ws.recv(timeout=8))
                if result.get('id') == request_id and result.get('op') == 'service_response':
                    if result.get('result') is not True:
                        raise RuntimeError(str(result.get('values', {})))
                    return result['values']

    def execute(self, service, args):
        value = self.call(service, args)
        if value.get('success') is not True:
            raise RuntimeError(f'{service}: {value}')
        return dict(status='service_acknowledged', physical_effect='unverified', service=service, response=value)

    def head(self, pitch=0.0, yaw=0.0, pre_check=False):
        args = dict(target_state=1, mode=0, req_id=uuid.uuid4().hex,
                    pre_check=pre_check, target_angles=[pitch, yaw], duration_ms=1000,
                    playback_rate=1.0, loop_enabled=False)
        if not pre_check:
            self.execute('/head_action', {**args, 'target_state':0, 'req_id':uuid.uuid4().hex})
        return self.execute('/head_action', args)

    def posture(self, mode, pre_check=True):
        return self.execute('/sm/action/lowlevel', dict(target_state=1, mode=mode,
            req_id=uuid.uuid4().hex, pre_check=pre_check))

    def emotion(self, mode, duration_ms=2000, target_state=1):
        return self.execute('/display_node/play_emotion', dict(target_state=target_state, mode=mode,
            req_id=uuid.uuid4().hex, duration_ms=duration_ms, pre_check=False))

    def light(self, rgb, brightness=40, duration_ms=1500, target_state=1):
        return self.execute('/light_node/control', dict(target_state=target_state, mode=0,
            req_id=uuid.uuid4().hex, duration_ms=duration_ms, pre_check=False,
            red=rgb[0], green=rgb[1], blue=rgb[2], brightness=brightness, speed=1.0))

    def sound(self, name):
        return self.execute('/set_speak', dict(target_state=1, mode=0,
            req_id=uuid.uuid4().hex, machine_language_name=name, pre_check=False))

    def say(self, text):
        return self.execute('/set_speak', dict(target_state=1, mode=1,
            req_id=uuid.uuid4().hex, human_language_text=text, pre_check=False))

    def stop_say(self):
        return self.execute('/set_speak', dict(target_state=0, mode=1,
            req_id=uuid.uuid4().hex, human_language_text='', pre_check=False))

    def stop_expression(self, emotion_mode=None):
        results = []
        for call in (
            lambda: self.light((0, 0, 0), brightness=0, duration_ms=100, target_state=0),
            lambda: self.stop_say(),
        ):
            try:
                results.append(call())
            except Exception as error:
                results.append({'status': 'stop_failed', 'error': str(error)})
        if emotion_mode is not None:
            try:
                results.append(self.emotion(emotion_mode, duration_ms=100, target_state=0))
            except Exception as error:
                results.append({'status': 'stop_failed', 'error': str(error)})
        return results

    def stop_following(self):
        return self.execute('/function/following', dict(target_state=0, mode=0,
            req_id=uuid.uuid4().hex, pre_check=False))

    def follow_check(self, mode=1):
        if mode not in (1, 4, 5):
            raise ValueError('Supported checks: follow, come-to-me, walk')
        return self.execute('/function/following', dict(target_state=1, mode=mode,
            req_id=uuid.uuid4().hex, pre_check=True, max_xvel=.1, stop_distance=1.0))

    def plan_path(self, target_name):
        if not isinstance(target_name,str) or not 1 <= len(target_name) <= 100:
            raise ValueError('A known map target name is required')
        return self.call('/get_path_to_target', dict(target_name=target_name))

#!/usr/bin/env python3
"""Vbot adapter implementing a bounded rosbridge JSON protocol subset.

Loopback only; access through SSH. No raw publish, actions, parameters,
firmware, shell execution, or unvalidated navigation. Uses vendor ROS types.
"""
import asyncio
import base64
import json
import time
import math
from pathlib import Path
import cv2
import numpy as np
import rclpy
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.utilities import get_message, get_service
from rosidl_runtime_py.convert import message_to_ordereddict
from rosidl_runtime_py.set_message import set_message_fields
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

READ_SERVICES = {
    '/function/context/get_context', '/display_node/get_supported_emotions',
    '/display_node/screen_state', '/light_node/status',
    '/ego_state_estimator/get_instant_status', '/dvr/get_session_list',
    '/get_lowstate', '/get_faults_count', '/get_faults_info',
    '/get_path_to_target', '/software/sm/get_fg_state',
    '/firmware_version/motor', '/firmware_version/servo', '/firmware_version/uwb',
    '/fan_node/speed', '/isp_adjust_state', '/touch_node/butt_enable_state',
    '/s100/get_init_state_trans_res', '/x5/get_init_state_trans_res',
}
READ_TOPICS = {
    '/bms_state', '/function/context/context_snapshot', '/system/sm_status',
    '/uwb/state', '/uwb/data', '/odometry', '/imu_raw', '/slam/status',
    '/perception/detections2d', '/perception/poses', '/depth/mono',
    '/lidar_points', '/path', '/servo/status', '/locomotion/status',
    '/display_node/status', '/touch_node/touch_state', '/navigation_status',
    '/datou/camera/jpeg', '/touch_node/touch_event',
    '/speech/listen_event', '/speech/wakeup_info', '/speech/command_word',
}
CONTROL_SERVICES = {'/sm/action/lowlevel', '/function/following', '/set_speak', '/light_node/control', '/display_node/play_emotion', '/head_action'}

def load_resources():
    emotions = {}
    sounds = set()
    for p in Path('/app/config/dags').rglob('*.json'):
        try:
            d = json.loads(p.read_text())
            for n in d.get('dag', {}).get('nodes', []):
                t, a = n.get('task', ''), n.get('args', {})
                if not isinstance(t, str) or not isinstance(a, dict):
                    continue
                if t.startswith('emotion.') and isinstance(a.get('emotion_id'), int):
                    emotions[t.split('.', 1)[1]] = a['emotion_id']
                sound = a.get('machine_language_name')
                if t == 'speak.MACHINE_LANGUAGE' and isinstance(sound, str) and sound:
                    sounds.add(sound)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return {'emotions': emotions, 'sounds': sorted(sounds), 'source': '/app/config/dags', 'execution_note': 'Display and sound only; no body DAG is executed'}

RESOURCES = load_resources()

def validate_call(service, args):
    if service in READ_SERVICES:
        return
    if service == '/speech_control' and args.get('request_type') in (1, 2, 5):
        return
    if service == '/sm/action/lowlevel':
        if args.get('mode') not in (1, 3, 4) or args.get('target_state') != 1:
            raise ValueError('Only stand, safe stop and safe laydown sequences are enabled')
        if args.get('mode') != 1 and not args.get('pre_check', False):
            raise ValueError('Safe stop/laydown are pre-check only pending physical validation')
        if args.get('action_path') or args.get('action_params_json'):
            raise ValueError('Custom action payloads are not enabled')
        return
    if service == '/head_action':
        if args.get('mode') != 0 or args.get('target_state') not in (0, 1):
            raise ValueError('Only bounded head angle control is enabled')
        angles = args.get('target_angles', [])
        if not isinstance(angles, list) or len(angles) != 2 or any(type(a) not in (float, int) or not math.isfinite(a) or abs(a) > .12 for a in angles):
            raise ValueError('Head pitch/yaw must each be finite and within 0.12 radians')
        if args.get('action_path') or args.get('loop_enabled') or args.get('duration_ms') != 1000:
            raise ValueError('Head trajectories/loops disabled; duration must be 1000 ms')
        return
    if service == '/function/following' and args.get('target_state') == 0:
        return
    if service == '/function/following' and args.get('pre_check') is True and args.get('target_state') == 1 and args.get('mode') in (1, 4, 5):
        if type(args.get('max_xvel')) not in (int, float) or not 0 < args['max_xvel'] <= .1:
            raise ValueError('Follow precheck speed must be <= 0.1 m/s')
        return
    if service == '/set_speak':
        if args.get('mode') == 0 and args.get('target_state') in (0, 1):
            if args.get('machine_language_name') in ('happy_short', 'confirm') and args.get('machine_language_name') in RESOURCES['sounds']:
                return
            raise ValueError('Only confirmed short built-in sounds happy_short/confirm are enabled')
        if args.get('mode') != 1 or args.get('target_state') not in (0, 1):
            raise ValueError('Unsupported speech mode')
        if args.get('machine_language_name') or len(args.get('human_language_text', '')) > 200:
            raise ValueError('Speech must be at most 200 characters, without an audio file path')
        return
    if service == '/display_node/play_emotion':
        if args.get('mode') not in set(RESOURCES['emotions'].values()) or args.get('target_state') not in (0, 1):
            raise ValueError('Emotion mode must exist in the onboard DAG catalogue')
        if not 1 <= args.get('duration_ms', 0) <= 3000:
            raise ValueError('Emotion must have a duration between 1 and 3000 ms')
        return
    if service == '/light_node/control':
        if args.get('mode') != 0 or args.get('target_state') not in (0, 1):
            raise ValueError('Only FIXEDCOLOR mode is enabled')
        if not 1 <= args.get('duration_ms', 0) <= 3000 or not 0 <= args.get('brightness', 0) <= 50:
            raise ValueError('Light must be bounded to 3 seconds and brightness <= 50')
        return
    raise ValueError('Service is inventoried but not enabled for execution: ' + service)

def plain(value):
    if hasattr(value, 'get_fields_and_field_types'):
        return plain(message_to_ordereddict(value))
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, 'tolist'):
        return plain(value.tolist())
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    return value

class Bridge:
    def __init__(self):
        self.node = rclpy.create_node('datou_agenticros_bridge')
        self.lock = asyncio.Lock()
        self.head_reset_task = None

    async def reset_head(self):
        await asyncio.sleep(1.5)
        try:
            async with self.lock:
                context = await self.call('/function/context/get_context', {})
                snap = context['snapshot']
                if snap['dag_status']['emergency_stop_active'] or snap['system_status']['joy_override_active']:
                    return
                args = dict(target_state=1, mode=0,
                    target_angles=[0.0, 0.0], duration_ms=1000, playback_rate=1.0,
                    loop_enabled=False, pre_check=False, req_id='companion-head-return')
                stopped = await self.call('/head_action', {**args, 'target_state':0})
                if stopped.get('success'):
                    await self.call('/head_action', args)
        except Exception as error:
            print('HEAD_RETURN_FAILED', str(error), flush=True)

    async def call(self, service, args):
        types = dict(self.node.get_service_names_and_types())
        if service not in types:
            raise ValueError('Service not present: ' + service)
        cls = get_service(types[service][0])
        req = cls.Request()
        set_message_fields(req, args)
        client = self.node.create_client(cls, service)
        try:
            if not client.service_is_ready():
                await asyncio.sleep(0.2)
            if not client.service_is_ready():
                raise TimeoutError('Service not ready; no request sent')
            future = client.call_async(req)
            deadline = time.monotonic() + 6
            while not future.done():
                if time.monotonic() > deadline:
                    future.cancel()
                    raise TimeoutError('Response timeout; execution outcome unknown; do not retry motion automatically')
                await asyncio.sleep(0.02)
            return plain(future.result())
        finally:
            self.node.destroy_client(client)

    def catalogue(self):
        services = []
        for name, types in self.node.get_service_names_and_types():
            if types[0].startswith('rcl_interfaces/'):
                continue
            item = {'name': name, 'type': types[0], 'enabled': name in READ_SERVICES | CONTROL_SERVICES}
            try:
                cls = get_service(types[0])
                item['request'] = cls.Request.get_fields_and_field_types()
                item['response'] = cls.Response.get_fields_and_field_types()
                item['constants'] = {k: getattr(cls.Request, k) for k in dir(cls.Request) if k.isupper() and isinstance(getattr(cls.Request, k), (int, str, float))}
            except Exception as e:
                item['schema_error'] = str(e)
            services.append(item)
        return {'services': services, 'topics': [{'name': n, 'types': ts, 'subscription_enabled': n in READ_TOPICS} for n, ts in self.node.get_topic_names_and_types()], 'scope': 'discovered schemas; enabled does not imply physical validation'}

    async def handler(self, ws):
        subs = {}
        async def send(value):
            try:
                await ws.send(json.dumps(value, allow_nan=False))
            except ConnectionClosed:
                pass
        try:
            async for raw in ws:
                q = json.loads(raw)
                op, topic = q.get('op'), q.get('topic')
                try:
                    if op == 'call_service':
                        name, args = q.get('service'), q.get('args') or {}
                        if name == '/rosapi/topics':
                            pairs = self.node.get_topic_names_and_types()
                            pairs.append(('/datou/camera/jpeg', ['sensor_msgs/msg/CompressedImage']))
                            result = {'topics': [p[0] for p in pairs], 'types': [p[1][0] for p in pairs]}
                        elif name == '/rosapi/services':
                            pairs = self.node.get_service_names_and_types()
                            result = {'services': [p[0] for p in pairs], 'types': [p[1][0] for p in pairs]}
                        elif name == '/datou/catalogue':
                            result = self.catalogue()
                        elif name == '/datou/resources':
                            result = RESOURCES
                        elif name == '/datou/actions':
                            result = {'actions': []}
                            for path in sorted(Path('/app/config/dags/actions').glob('*.json')):
                                try:
                                    result['actions'].append({'file': path.name, 'config': json.loads(path.read_text()), 'execution_enabled': False})
                                except (OSError, ValueError):
                                    pass
                        else:
                            validate_call(name, args)
                            async with self.lock:
                                if name in ('/sm/action/lowlevel', '/head_action') and not args.get('pre_check', False):
                                    context = await self.call('/function/context/get_context', {})
                                    snap = context.get('snapshot') or context.get('context')
                                    if not isinstance(snap, dict):
                                        raise ValueError('Unrecognized context schema; stand not sent')
                                    if snap['dag_status']['emergency_stop_active'] or not snap['system_status']['robot_is_static'] or snap['system_status'].get('joy_override_active') or snap['system_status'].get('agent_enable'):
                                        raise ValueError('Robot must be static without active emergency stop')
                                    if snap['function_status']['follow_status']['status'] or snap['function_status']['nav_status']['status']:
                                        raise ValueError('Stop active following/navigation before stand')
                                result = await self.call(name, args)
                                if name == '/head_action' and not args.get('pre_check', False) and result.get('success'):
                                    if self.head_reset_task:
                                        self.head_reset_task.cancel()
                                    self.head_reset_task = asyncio.create_task(self.reset_head()) if args.get('target_state') == 1 else None
                        await send({'op': 'service_response', 'id': q.get('id'), 'service': name, 'result': True, 'values': result})
                    elif op == 'subscribe':
                        if topic not in READ_TOPICS:
                            raise ValueError('Topic is inventoried but subscription not enabled: ' + str(topic))
                        if topic in subs:
                            continue
                        if len(subs) >= 4:
                            raise ValueError('Maximum 4 subscriptions per connection')
                        source = '/image_left_raw/nv12_quarter' if topic == '/datou/camera/jpeg' else topic
                        types = dict(self.node.get_topic_names_and_types())
                        cls = get_message(types[source][0])
                        last = [0.0]
                        def callback(msg, out=topic, stamp=last):
                            if time.monotonic() - stamp[0] < 1:
                                return
                            stamp[0] = time.monotonic()
                            try:
                                if out == '/datou/camera/jpeg':
                                    data = np.frombuffer(bytes(msg.data), dtype=np.uint8)
                                    if msg.encoding.lower() != 'nv12':
                                        raise ValueError('Expected NV12 camera, got ' + msg.encoding)
                                    frame = cv2.cvtColor(data.reshape((-1, msg.width)), cv2.COLOR_YUV2BGR_NV12)
                                    ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                                    if not ok:
                                        raise ValueError('JPEG encoding failed')
                                    value = {'header': plain(msg.header), 'format': 'jpeg', 'data': base64.b64encode(jpeg).decode()}
                                else:
                                    value = plain(msg)
                                task = asyncio.create_task(send({'op': 'publish', 'topic': out, 'msg': value}))
                                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                            except Exception as e:
                                print('Subscription error:', out, str(e), flush=True)
                        subs[topic] = self.node.create_subscription(cls, source, callback, qos_profile_sensor_data)
                    elif op == 'unsubscribe':
                        if topic in subs:
                            self.node.destroy_subscription(subs.pop(topic))
                    else:
                        raise ValueError('Unsupported operation: ' + str(op))
                except Exception as e:
                    if op == 'call_service':
                        await send({'op': 'service_response', 'id': q.get('id'), 'service': q.get('service'), 'result': False, 'values': {'error': str(e)}})
                    else:
                        await send({'op': 'status', 'id': q.get('id'), 'level': 'error', 'msg': str(e)})
        finally:
            for sub in subs.values():
                self.node.destroy_subscription(sub)

    async def spin(self):
        while rclpy.ok():
            rclpy.spin_once(self.node, timeout_sec=0)
            await asyncio.sleep(0.01)

async def main():
    rclpy.init()
    bridge = Bridge()
    async with serve(bridge.handler, '127.0.0.1', 9091, max_size=2**20):
        print('DATOU_AGENTICROS_READY 127.0.0.1:9091', flush=True)
        await bridge.spin()

if __name__ == '__main__':
    asyncio.run(main())

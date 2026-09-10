"""Named native skills. Runtime bridge remains the authority on execution scope."""
from native import NativeClient

SKILLS = {
    'status': ('/function/context/get_context', {}, 'read'),
    'resources': ('/datou/resources', {}, 'read'),
    'actions': ('/datou/actions', {}, 'read'),
    'catalogue': ('/datou/catalogue', {}, 'read'),
    'screen': ('/display_node/screen_state', {}, 'read'),
    'emotions': ('/display_node/get_supported_emotions', {}, 'read'),
    'lights': ('/light_node/status', {}, 'read'),
    'lowstate': ('/get_lowstate', {}, 'read'),
    'faults': ('/get_faults_count', {}, 'read'),
    'recordings': ('/dvr/get_session_list', {'max_results':20}, 'read'),
    'fan': ('/fan_node/speed', {'fan_idx':0}, 'read'),
    'touch_enabled': ('/touch_node/butt_enable_state', {}, 'read'),
    'chat_status': ('/speech_control', {'request_type':2}, 'read'),
    'voice_commands': ('/speech_control', {'request_type':1}, 'read'),
    'continuous_voice': ('/speech_control', {'request_type':5}, 'read'),
    'stand_check': ('/sm/action/lowlevel', {'target_state':1,'mode':1,'pre_check':True}, 'precheck'),
    'stop_check': ('/sm/action/lowlevel', {'target_state':1,'mode':3,'pre_check':True}, 'precheck'),
    'laydown_check': ('/sm/action/lowlevel', {'target_state':1,'mode':4,'pre_check':True}, 'precheck'),
    'head_check': ('/head_action', {'target_state':1,'mode':0,'target_angles':[0.0,.08],
                                 'duration_ms':1000,'loop_enabled':False,'playback_rate':1.0,'pre_check':True}, 'precheck'),
    'follow_check': ('/function/following', {'target_state':1,'mode':1,'pre_check':True,'max_xvel':.1,'stop_distance':1.0}, 'precheck'),
    'recall_check': ('/function/following', {'target_state':1,'mode':4,'pre_check':True,'max_xvel':.1,'stop_distance':1.0}, 'precheck'),
    'walk_check': ('/function/following', {'target_state':1,'mode':5,'pre_check':True,'max_xvel':.1,'stop_distance':1.0}, 'precheck'),
}


def run_skill(name, client=None):
    if name not in SKILLS:
        raise ValueError('Unknown skill; use list to inspect named skills')
    service, args, kind = SKILLS[name]
    value = (client or NativeClient()).call(service, args)
    if value.get('success') is False:
        raise RuntimeError(str(value))
    return dict(skill=name, kind=kind, response=value)


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('skill', choices=['list'] + list(SKILLS))
    args = parser.parse_args()
    print(json.dumps(SKILLS if args.skill == 'list' else run_skill(args.skill),ensure_ascii=False,indent=2))

"""Load only the pure validation function; ROS hardware is not imported."""
import ast
import math
import unittest
from pathlib import Path

source=Path(__file__).resolve().parents[3]/'scripts'/'agenticros_bridge.py'
tree=ast.parse(source.read_text())
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='validate_call')
namespace={'math':math,'READ_SERVICES':set(), 'RESOURCES':{'emotions':{'happy':4},'sounds':['happy_short','confirm']}}
exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),namespace)
validate=namespace['validate_call']


class PolicyTests(unittest.TestCase):
    def test_head_rejects_nan_and_unbounded_motion(self):
        base=dict(target_state=1,mode=0,duration_ms=1000,target_angles=[0,.08])
        validate('/head_action',base)
        for fields in [dict(target_angles=[0,float('nan')]),dict(target_angles=[0,.3]),dict(action_path='x.csv'),dict(loop_enabled=True),dict(duration_ms=-1)]:
            with self.assertRaises(ValueError):validate('/head_action',{**base,**fields})

    def test_unverified_laydown_cannot_execute(self):
        validate('/sm/action/lowlevel',dict(target_state=1,mode=4,pre_check=True))
        with self.assertRaises(ValueError):validate('/sm/action/lowlevel',dict(target_state=1,mode=4,pre_check=False))

    def test_queries_do_not_enable_cloud_agent_or_movement(self):
        validate('/speech_control',dict(request_type=2))
        for service,args in [('/speech_control',dict(request_type=4,enable=True)),('/function/following',dict(target_state=1,mode=1)),('/execute_x5_command',dict(command='anything')),('/function_input',dict(dag='{}'))]:
            with self.assertRaises(ValueError):validate(service,args)

import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sensors import Sensors
from adapter import PetAdapter


class SensorsTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.s = Sensors(clock=lambda: self.now)
        self.context = dict(dag_status={'emergency_stop_active':False},
            system_status={'joy_override_active':False,'robot_is_static':True},
            function_status={'follow_status':{'status':0},'nav_status':{'status':0}})
        self.s.ingest('/function/context/context_snapshot', self.context)
        self.s.ingest('/bms_state', dict(soc_percent=70,is_charger_connected=False,alarm=0,request_shutdown=False))

    def test_no_person_without_real_evidence(self):
        self.assertFalse(self.s.observation()['present'])
        self.s.ingest('/perception/detections2d', {'detections':[{'results':[{'hypothesis':{'class_id':'0','score':.99}}]}]})
        self.assertFalse(self.s.observation()['present'])
        self.s.ingest('/perception/poses', {'class_id':0,'score':.9})
        self.assertTrue(self.s.observation()['present'])

    def test_touch_first_sample_and_held_press_do_not_repeat(self):
        def touch(state): self.s.ingest('/touch_node/touch_state', dict(source=0,idx=0,state=state))
        touch(1)
        self.assertFalse(self.s.take_touch())
        touch(0);touch(1)
        self.assertTrue(self.s.take_touch())
        touch(1)
        self.assertFalse(self.s.take_touch())
        self.assertTrue(self.s.observation()['present'])

    def test_stale_battery_invalidates_whole_observation(self):
        self.now += 6
        self.assertIsNone(self.s.observation())

    def test_manual_override_and_active_agent_block_actions(self):
        self.context['system_status']['agent_enable']=True
        self.s.ingest('/function/context/context_snapshot',self.context)
        self.assertTrue(self.s.observation()['busy'])

    def test_duplicate_frames_do_not_refresh_presence(self):
        msg={'header':{'stamp':{'sec':123,'nanosec':1}},'class_id':0,'score':.9}
        self.s.ingest('/perception/poses',msg)
        self.now+=10
        self.s.ingest('/perception/poses',msg)
        self.assertFalse(self.s.status()['person_detected'])

    def test_old_touch_event_dropped(self):
        self.s.ingest('/touch_node/touch_event',{'source':0,'idx':0,'state':10})
        self.now+=3
        self.assertFalse(self.s.take_touch())


class DispatchTests(unittest.TestCase):
    def test_cancellation_between_steps_prevents_head(self):
        class Fake:
            def call(self,*a):return {'emotions':{'LISTEN':44}}
            def light(self,*a):return {'status':'ok'}
            def emotion(self,*a):raise AssertionError('cancelled before expression')
            def head(self,*a):raise AssertionError('cancelled before head')
        calls=[]
        def dispatch(fn):
            calls.append(1)
            return fn() if len(calls)==1 else None
        result=PetAdapter(Fake(),head=True).execute('invite',dispatch)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(len(result['steps']),1)


if __name__=='__main__':unittest.main()

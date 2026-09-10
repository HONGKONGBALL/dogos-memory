"""Exercise existing-identity voice activation and changes in real gate inputs."""

import json
from unittest.mock import patch

import pytest

from native_agent.runtime.gates import evaluate_gate
from native_agent.runtime.s100_admin import AdminError, enable_existing_voice, restore_voice
from native_agent.tests.test_runtime import FakeVoiceRos, valid_snapshot


def snapshot_for(ros, *, charging=False, **kwargs):
    snapshot = valid_snapshot()
    snapshot['x5']['identity_state'] = None
    snapshot['context']['system_status']['agent_enable'] = ros.agent
    snapshot['speech'].update(
        chat_function_status=ros.chat,
        cmd_recognition_mode=ros.command_mode,
        continuous_cmd_status=ros.continuous,
    )
    if charging:
        snapshot['battery'].update(is_charger_connected=True, charge_state=2)
    snapshot['gate'] = evaluate_gate(snapshot, **kwargs).to_dict()
    return snapshot


def test_existing_identity_activates_without_release_and_restores_switches(tmp_path):
    ros = FakeVoiceRos()
    ros.command_mode = 2
    ros.continuous = True
    with (
        patch('native_agent.runtime.s100_admin.STATE_ROOT', tmp_path),
        patch('native_agent.runtime.s100_admin.collect_snapshot',
              side_effect=lambda ros, **kw: snapshot_for(ros, **kw)),
    ):
        result = enable_existing_voice(ros, dog_id='datou')
        assert result['status'] == 'ready'
        assert result['profile'] == 'existing-identity-voice'
        assert result['voice_round_trip'] == 'not_tested'
        assert result['revision'] is None
        assert result['voice'] == {
            'agent_enable': True, 'chat_function_status': True,
            'cmd_recognition_mode': 0, 'continuous_cmd_status': False,
        }
        assert {name for name, _ in ros.calls} == {'/agent/enable', '/speech_control'}
        saved = json.loads((tmp_path / 'datou.json').read_text())
        assert saved['voice']['cmd_recognition_mode'] == 2
        assert restore_voice(ros, dog_id='datou')['status'] == 'restored'
    assert not ros.agent and not ros.chat
    assert ros.command_mode == 2 and ros.continuous


def test_charging_blocks_before_any_voice_switch(tmp_path):
    ros = FakeVoiceRos()
    with (
        patch('native_agent.runtime.s100_admin.STATE_ROOT', tmp_path),
        patch('native_agent.runtime.s100_admin.collect_snapshot',
              side_effect=lambda ros, **kw: snapshot_for(ros, charging=True, **kw)),
        pytest.raises(AdminError, match='charger_disconnected'),
    ):
        enable_existing_voice(ros, dog_id='datou')
    assert ros.calls == []
    assert not (tmp_path / 'datou.json').exists()


def test_state_becoming_unsafe_after_switch_restores_original(tmp_path):
    ros = FakeVoiceRos()
    reads = 0

    def collect(ros, **kwargs):
        nonlocal reads
        reads += 1
        return snapshot_for(ros, charging=reads > 1, **kwargs)

    with (
        patch('native_agent.runtime.s100_admin.STATE_ROOT', tmp_path),
        patch('native_agent.runtime.s100_admin.collect_snapshot', side_effect=collect),
        pytest.raises(AdminError, match='charger_disconnected'),
    ):
        enable_existing_voice(ros, dog_id='datou')
    assert not ros.agent and not ros.chat and not ros.continuous
    assert ros.command_mode == 0
    assert not (tmp_path / 'datou.json').exists()

"""Regressions from the September 10 single-dog firmware check."""

import json
from unittest.mock import patch

import pytest

from native_agent.runtime.gates import evaluate_gate
from native_agent.runtime.s100_admin import RosAccess, _safe_battery, restore_voice
from native_agent.tests.test_runtime import FakeVoiceRos, valid_snapshot
from native_agent.tests.test_voice_start import snapshot_for


@pytest.mark.parametrize("current,connected,allowed", [
    (-1580, False, True), (150, False, True), (151, False, False),
    (None, False, False), (-1580, True, False),
])
def test_older_bms_needs_unplugged_and_noncharging_current(current, connected, allowed):
    snapshot = valid_snapshot()
    raw = dict(snapshot["battery"])
    raw.pop("charge_state")
    raw.update(current_ma=current, is_charger_connected=connected)
    snapshot["battery"] = _safe_battery(raw)
    result = evaluate_gate(snapshot, purpose="activate")
    assert ("not_actively_charging" not in result.blockers) == allowed


def test_present_but_invalid_charge_state_does_not_use_legacy_fallback():
    snapshot = valid_snapshot()
    snapshot["battery"] = _safe_battery({
        **snapshot["battery"], "charge_state": None, "current_ma": -1580,
    })
    assert "not_actively_charging" in evaluate_gate(snapshot, purpose="activate").blockers


def test_client_only_service_is_not_advertised_as_available():
    class Graph:
        def get_service_names_and_types(self):
            return [("/agent/enable", ["std_srvs/srv/SetBool"])]

        def get_node_names_and_namespaces(self):
            return [("app_agent", "/")]

        def get_service_names_and_types_by_node(self, name, namespace):
            return [("/agent/get_jpeg_image", ["std_srvs/srv/Trigger"])]

    ros = object.__new__(RosAccess)
    ros.node = Graph()
    assert "/agent/enable" not in ros.service_types()
    assert "/agent/get_jpeg_image" in ros.service_types()


def test_restore_unchanged_state_does_not_need_unavailable_switch(tmp_path):
    ros = FakeVoiceRos()
    saved = tmp_path / "datou.json"
    saved.write_text(json.dumps({"schema_version": 1, "dog_id": "datou", "voice": {
        "agent_enable": False, "chat_function_status": False,
        "cmd_recognition_mode": 0, "continuous_cmd_status": False,
    }}))
    with (
        patch("native_agent.runtime.s100_admin.STATE_ROOT", tmp_path),
        patch("native_agent.runtime.s100_admin.collect_snapshot",
              side_effect=lambda ros, **kw: snapshot_for(ros, **kw)),
    ):
        assert restore_voice(ros, dog_id="datou")["status"] == "restored"
    assert ros.calls == []
    assert not saved.exists()

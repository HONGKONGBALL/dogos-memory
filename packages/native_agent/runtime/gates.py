"""Fail-closed preflight gates for native Agent installation and activation.

The functions in this module are deliberately free of ROS and filesystem I/O so
that the exact decisions used on S100 can be exercised on a development host.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


EXPECTED_SERVICE_TYPES = {
    "/agent/enable": "std_srvs/srv/SetBool",
    "/execute_x5_command": "software_msgs/srv/ExecuteCommand",
    "/function/context/get_context": "function_msgs/srv/GetContext",
    "/get_faults_info": "software_msgs/srv/GetFaultsInfo",
    "/speech_control": "speech_msgs/srv/SpeechControl",
    "/write_x5_file": "software_msgs/srv/WriteFile",
}

MIN_X5_FREE_BYTES = 32 * 1024 * 1024
MIN_STATIC_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class GateReport:
    purpose: str
    ok: bool
    checks: tuple[str, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "purpose": self.purpose,
            "ok": self.ok,
            "checks": list(self.checks),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _add(condition: bool, code: str, checks: list[str], blockers: list[str]) -> None:
    (checks if condition else blockers).append(code)


def evaluate_gate(
    snapshot: Mapping[str, object],
    *,
    purpose: str,
    expected_revision: str | None = None,
    minimum_battery_percent: float = 40.0,
) -> GateReport:
    """Evaluate an ``inspect`` snapshot for install or voice activation.

    ``install`` permits a connected charger because it performs no body action.
    ``activate`` is stricter because the vendor Agent owns native action tools.
    The first activation profile keeps command-word and continuous-recognition
    routes off, but this is not a substitute for a vendor tool hook.
    """

    if purpose not in {"inspect", "install", "activate"}:
        raise ValueError("purpose must be inspect, install, or activate")
    if (
        isinstance(minimum_battery_percent, bool)
        or not isinstance(minimum_battery_percent, (int, float))
        or not 0 <= float(minimum_battery_percent) <= 100
    ):
        raise ValueError("minimum_battery_percent must be between 0 and 100")

    checks: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    services = _mapping(snapshot.get("services"))
    required = (
        {"/function/context/get_context", "/execute_x5_command"}
        if purpose == "inspect"
        else set(EXPECTED_SERVICE_TYPES)
    )
    for name in sorted(required):
        _add(
            services.get(name) == EXPECTED_SERVICE_TYPES[name],
            f"service_type:{name}",
            checks,
            blockers,
        )

    context = _mapping(snapshot.get("context"))
    dag = _mapping(context.get("dag_status"))
    functions = _mapping(context.get("function_status"))
    system = _mapping(context.get("system_status"))
    follow = _mapping(functions.get("follow_status"))
    navigation = _mapping(functions.get("nav_status"))

    _add(dag.get("execution_state") == 0, "dag_idle", checks, blockers)
    _add(not bool(dag.get("current_dag")), "dag_name_empty", checks, blockers)
    _add(
        dag.get("emergency_stop_active") is False,
        "no_emergency_stop",
        checks,
        blockers,
    )
    _add(system.get("robot_is_static") is True, "robot_static", checks, blockers)
    static_seconds = _number(system.get("static_duration"))
    _add(
        static_seconds is not None and static_seconds >= MIN_STATIC_SECONDS,
        "static_duration_fresh",
        checks,
        blockers,
    )
    _add(system.get("joy_override_active") is False, "no_joy_override", checks, blockers)
    _add(follow.get("status") == 0, "following_idle", checks, blockers)
    _add(navigation.get("status") == 0, "navigation_idle", checks, blockers)

    x5 = _mapping(snapshot.get("x5"))
    _add(x5.get("probe_success") is True, "x5_probe", checks, blockers)
    _add(x5.get("harness_active") is True, "harness_active", checks, blockers)
    free_bytes = _number(x5.get("free_bytes"))
    _add(
        free_bytes is not None and free_bytes >= MIN_X5_FREE_BYTES,
        "x5_free_space",
        checks,
        blockers,
    )
    competitors = x5.get("competing_agents")
    _add(
        isinstance(competitors, list) and len(competitors) == 0,
        "single_agent_topology",
        checks,
        blockers,
    )

    if purpose in {"install", "activate"}:
        faults = _mapping(snapshot.get("faults"))
        current_faults = faults.get("current")
        _add(faults.get("error_code") == 0, "fault_query_ok", checks, blockers)
        _add(
            isinstance(current_faults, list) and len(current_faults) == 0,
            "no_current_faults",
            checks,
            blockers,
        )

        battery = _mapping(snapshot.get("battery"))
        _add(battery.get("alarm") == 0, "battery_alarm_clear", checks, blockers)
        _add(
            battery.get("request_shutdown") is False,
            "battery_not_requesting_shutdown",
            checks,
            blockers,
        )

    if purpose == "install":
        _add(x5.get("memory_dir_ready") is True, "x5_memory_dir", checks, blockers)
        _add(x5.get("agents_parent_ready") is True, "x5_agents_parent", checks, blockers)
        speech = _mapping(snapshot.get("speech"))
        _add(speech.get("query_success") is True, "speech_queries_ok", checks, blockers)
        _add(system.get("agent_enable") is False, "agent_disabled_for_install", checks, blockers)
        _add(
            speech.get("chat_function_status") is False,
            "chat_disabled_for_install",
            checks,
            blockers,
        )
        _add(
            speech.get("cmd_recognition_mode") == 0,
            "command_recognition_disabled_for_install",
            checks,
            blockers,
        )
        _add(
            speech.get("continuous_cmd_status") is False,
            "continuous_commands_disabled_for_install",
            checks,
            blockers,
        )
        if _mapping(snapshot.get("battery")).get("is_charger_connected") is True:
            warnings.append("install_while_charging_no_agent_activation")

    if purpose == "activate":
        battery = _mapping(snapshot.get("battery"))
        percent = _number(battery.get("soc_percent"))
        _add(
            percent is not None and percent >= float(minimum_battery_percent),
            "battery_above_activation_threshold",
            checks,
            blockers,
        )
        _add(
            battery.get("is_charger_connected") is False,
            "charger_disconnected",
            checks,
            blockers,
        )
        # Older firmware lacks charge_state; its BMS defines >150 mA as
        # effective charging. Require both unplugged state and a fresh current.
        current_ma = _number(battery.get("current_ma"))
        legacy_not_charging = (
            battery.get("charge_state_supported") is False
            and battery.get("charge_state") is None
            and battery.get("is_charger_connected") is False
            and current_ma is not None
            and current_ma <= 150
        )
        _add(
            battery.get("charge_state") in {0, 1, 3} or legacy_not_charging,
            "not_actively_charging",
            checks,
            blockers,
        )
        _add(system.get("wakeup_turn") is True, "local_wakeup_turn_enabled", checks, blockers)
        _add(system.get("network_state") in {1, 2}, "cloud_network_available", checks, blockers)

        speech = _mapping(snapshot.get("speech"))
        _add(speech.get("query_success") is True, "speech_queries_ok", checks, blockers)

        if expected_revision is not None:
            state = _mapping(x5.get("identity_state"))
            _add(
                state.get("current_revision") == expected_revision,
                "identity_revision_matches",
                checks,
                blockers,
            )
            _add(
                state.get("restart_required") is False,
                "identity_restart_complete",
                checks,
                blockers,
            )
        else:
            warnings.append("existing_identity_not_managed_by_this_release")

        if x5.get("policy_hook_enforced") is not True:
            warnings.append("native_action_policy_hook_not_enforced")

    return GateReport(
        purpose=purpose,
        ok=not blockers,
        checks=tuple(checks),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )

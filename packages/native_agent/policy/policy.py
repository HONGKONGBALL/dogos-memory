"""Pure, fail-closed policy evaluation for native Vbot Agent tools.

Loading the JSON allowlist is the only I/O boundary.  ``evaluate_action`` uses
only its arguments, does not read a clock, and never calls robot interfaces.
Callers must pass a monotonic timestamp in the same clock domain as the state
timestamps.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import AbstractSet, Final, Mapping


SCHEMA_VERSION: Final = 1
DEFAULT_ALLOWLIST_PATH: Final = Path(__file__).with_name("action_allowlist.json")
TOOL_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
APPROVER_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
NONCE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
MAX_ARGUMENT_BYTES: Final = 4 * 1024
MAX_APPROVAL_LIFETIME_SECONDS: Final = 120.0
_APPROVAL_DOMAIN: Final = b"vbot-action-approval-v1\x00"


class RunMode(str, Enum):
    """Operating modes exposed by the control plane."""

    IDENTITY_ONLY = "identity-only"
    SUPERVISED_DEMO = "supervised-demo"
    AUTONOMOUS_SAFE = "autonomous-safe"


class ToolRisk(str, Enum):
    """Risk classes used by the checked-in action allowlist."""

    OBSERVE = "observe"
    COMMUNICATE = "communicate"
    DATA_WRITE = "data-write"
    DEVICE_CONTROL = "device-control"
    EXPRESSIVE = "expressive"
    BODY_MOTION = "body-motion"
    NAVIGATION = "navigation"
    JUMP = "jump"
    PRIVACY_CAPTURE = "privacy-capture"
    NETWORK_EGRESS = "network-egress"


class PolicyConfigError(ValueError):
    """Stable configuration error suitable for startup diagnostics."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class _DuplicateJsonKey(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


@dataclass(frozen=True, slots=True)
class RobotState:
    """State snapshot used to decide whether a physical action may start.

    ``None`` means unknown.  Required unknown values reject physical actions.
    ``last_physical_action_at_s`` is the start or completion time chosen by the
    caller for the global physical-action cooldown.
    """

    battery_percent: float | None = None
    charging: bool | None = None
    fault: bool | None = None
    stationary: bool | None = None
    sensors_observed_at_s: float | None = None
    person_present: bool | None = None
    action_busy: bool | None = None
    last_physical_action_at_s: float | None = None
    physical_action_history_known: bool | None = None


@dataclass(frozen=True, slots=True)
class ActionApproval:
    """Short-lived approval bound to one exact tool, mode, and argument object."""

    intent_sha256: str
    approved_by: str
    issued_at_s: float
    expires_at_s: float
    nonce: str
    signature: str


@dataclass(frozen=True, slots=True)
class ActionIntent:
    """A proposed native tool invocation before any robot call is made."""

    tool: str
    mode: RunMode | str
    arguments: Mapping[str, object] = field(default_factory=dict)
    approval: ActionApproval | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Auditable result returned by :func:`evaluate_action`."""

    allowed: bool
    tool: str
    mode: str
    risk: ToolRisk | None
    risk_level: int | None
    intent_sha256: str | None
    reason_codes: tuple[str, ...]

    @property
    def reason_code(self) -> str:
        """Return the primary reason while preserving all failures separately."""

        return self.reason_codes[0]


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Gates shared by every tool in one risk class."""

    risk: ToolRisk
    risk_level: int
    allowed_modes: frozenset[RunMode]
    physical: bool
    minimum_battery_percent: float | None
    allow_while_charging: bool
    requires_fault_free: bool
    requires_idle: bool
    requires_stationary: bool
    max_sensor_age_seconds: float | None
    requires_person_present: bool
    cooldown_seconds: float
    supervisor_approval_modes: frozenset[RunMode]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """One allowlisted tool and its risk class."""

    tool: str
    risk: ToolRisk


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Immutable, parsed representation of ``action_allowlist.json``."""

    schema_version: int
    risks: tuple[RiskPolicy, ...]
    tools: tuple[ToolPolicy, ...]

    def risk_policy(self, risk: ToolRisk) -> RiskPolicy | None:
        for rule in self.risks:
            if rule.risk is risk:
                return rule
        return None

    def tool_policy(self, tool: str) -> ToolPolicy | None:
        for rule in self.tools:
            if rule.tool == tool:
                return rule
        return None


RISK_KEYS: Final = {
    "risk_level",
    "allowed_modes",
    "physical",
    "minimum_battery_percent",
    "allow_while_charging",
    "requires_fault_free",
    "requires_idle",
    "requires_stationary",
    "max_sensor_age_seconds",
    "requires_person_present",
    "cooldown_seconds",
    "supervisor_approval_modes",
}


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise PolicyConfigError("invalid_type", f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise PolicyConfigError("invalid_key", f"{label} keys must be strings")
    return value


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _require_exact_keys(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise PolicyConfigError(
            "invalid_keys",
            f"{label}: expected {sorted(expected)}, got {sorted(actual)}",
        )


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyConfigError("invalid_type", f"{label} must be a boolean")
    return value


def _require_number(
    value: object,
    label: str,
    *,
    minimum: float,
    maximum: float | None = None,
    nullable: bool = False,
) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyConfigError("invalid_type", f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise PolicyConfigError("invalid_number", label)
    if maximum is not None and number > maximum:
        raise PolicyConfigError("invalid_number", label)
    return number


def _require_integer(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PolicyConfigError("invalid_integer", label)
    return value


def _parse_modes(value: object, label: str) -> frozenset[RunMode]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyConfigError("invalid_type", f"{label} must be a string array")
    try:
        modes = frozenset(RunMode(item) for item in value)
    except ValueError as error:
        raise PolicyConfigError("invalid_mode", f"{label}: {error}") from error
    if len(modes) != len(value):
        raise PolicyConfigError("duplicate_mode", label)
    return modes


def parse_action_allowlist(document: object) -> PolicyConfig:
    """Validate and parse an already-decoded allowlist document."""

    root = _require_mapping(document, "root")
    _require_exact_keys(root, {"schema_version", "risk_profiles", "tools"}, "root")
    if type(root["schema_version"]) is not int or root["schema_version"] != SCHEMA_VERSION:
        raise PolicyConfigError(
            "unsupported_schema", f"expected {SCHEMA_VERSION}, got {root['schema_version']}"
        )

    raw_risks = _require_mapping(root["risk_profiles"], "risk_profiles")
    risks: list[RiskPolicy] = []
    for risk_name, raw_rule in raw_risks.items():
        try:
            risk = ToolRisk(risk_name)
        except ValueError as error:
            raise PolicyConfigError("unknown_risk", risk_name) from error
        rule = _require_mapping(raw_rule, f"risk_profiles.{risk_name}")
        _require_exact_keys(rule, RISK_KEYS, f"risk_profiles.{risk_name}")
        allowed_modes = _parse_modes(
            rule["allowed_modes"], f"risk_profiles.{risk_name}.allowed_modes"
        )
        if not allowed_modes:
            raise PolicyConfigError("empty_modes", risk_name)
        supervisor_modes = _parse_modes(
            rule["supervisor_approval_modes"],
            f"risk_profiles.{risk_name}.supervisor_approval_modes",
        )
        if not supervisor_modes.issubset(allowed_modes):
            raise PolicyConfigError("invalid_supervisor_modes", risk_name)
        risks.append(
            RiskPolicy(
                risk=risk,
                risk_level=_require_integer(
                    rule["risk_level"], f"risk_profiles.{risk_name}.risk_level", minimum=0
                ),
                allowed_modes=allowed_modes,
                physical=_require_bool(
                    rule["physical"], f"risk_profiles.{risk_name}.physical"
                ),
                minimum_battery_percent=_require_number(
                    rule["minimum_battery_percent"],
                    f"risk_profiles.{risk_name}.minimum_battery_percent",
                    minimum=0.0,
                    maximum=100.0,
                    nullable=True,
                ),
                allow_while_charging=_require_bool(
                    rule["allow_while_charging"],
                    f"risk_profiles.{risk_name}.allow_while_charging",
                ),
                requires_fault_free=_require_bool(
                    rule["requires_fault_free"],
                    f"risk_profiles.{risk_name}.requires_fault_free",
                ),
                requires_idle=_require_bool(
                    rule["requires_idle"], f"risk_profiles.{risk_name}.requires_idle"
                ),
                requires_stationary=_require_bool(
                    rule["requires_stationary"],
                    f"risk_profiles.{risk_name}.requires_stationary",
                ),
                max_sensor_age_seconds=_require_number(
                    rule["max_sensor_age_seconds"],
                    f"risk_profiles.{risk_name}.max_sensor_age_seconds",
                    minimum=0.0,
                    nullable=True,
                ),
                requires_person_present=_require_bool(
                    rule["requires_person_present"],
                    f"risk_profiles.{risk_name}.requires_person_present",
                ),
                cooldown_seconds=_require_number(
                    rule["cooldown_seconds"],
                    f"risk_profiles.{risk_name}.cooldown_seconds",
                    minimum=0.0,
                )
                or 0.0,
                supervisor_approval_modes=supervisor_modes,
            )
        )

    if {rule.risk for rule in risks} != set(ToolRisk):
        missing = sorted(risk.value for risk in set(ToolRisk) - {rule.risk for rule in risks})
        raise PolicyConfigError("missing_risk_profiles", ", ".join(missing))

    raw_tools = _require_mapping(root["tools"], "tools")
    tools: list[ToolPolicy] = []
    available_risks = {rule.risk for rule in risks}
    for tool, risk_name in raw_tools.items():
        if not TOOL_NAME_RE.fullmatch(tool):
            raise PolicyConfigError("invalid_tool_name", tool)
        if not isinstance(risk_name, str):
            raise PolicyConfigError("invalid_type", f"tools.{tool} must be a string")
        try:
            risk = ToolRisk(risk_name)
        except ValueError as error:
            raise PolicyConfigError("unknown_risk", f"tools.{tool}: {risk_name}") from error
        if risk not in available_risks:
            raise PolicyConfigError("missing_risk_profile", risk.value)
        tools.append(ToolPolicy(tool=tool, risk=risk))
    if not tools:
        raise PolicyConfigError("empty_tools", "at least one tool must be allowlisted")

    return PolicyConfig(
        schema_version=SCHEMA_VERSION,
        risks=tuple(sorted(risks, key=lambda item: item.risk.value)),
        tools=tuple(sorted(tools, key=lambda item: item.tool)),
    )


def load_action_allowlist(path: Path | str = DEFAULT_ALLOWLIST_PATH) -> PolicyConfig:
    """Load the checked-in allowlist or another explicitly supplied JSON file."""

    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            document = json.load(
                handle,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
    except _DuplicateJsonKey as error:
        raise PolicyConfigError("duplicate_json_key", error.key) from error
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise PolicyConfigError("allowlist_read_failed", str(error)) from error
    return parse_action_allowlist(document)


def classify_tool(tool: str, policy: PolicyConfig) -> ToolRisk | None:
    """Return the allowlisted risk class, or ``None`` for a denied tool."""

    rule = policy.tool_policy(tool)
    return None if rule is None else rule.risk


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _normalise_json_value(value: object, *, depth: int = 0) -> object:
    if depth > 5:
        raise ValueError("arguments are too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 2_048:
            raise ValueError("argument string is too long")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("argument number must be finite")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            raise ValueError("argument array is too long")
        return [_normalise_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise ValueError("argument object has too many fields")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not TOOL_NAME_RE.fullmatch(key):
                raise ValueError("argument field names must use lower snake case")
            result[key] = _normalise_json_value(item, depth=depth + 1)
        return result
    raise ValueError(f"unsupported argument type: {type(value).__name__}")


def _canonical_intent(intent: ActionIntent) -> tuple[bytes, str]:
    if not isinstance(intent.tool, str) or not TOOL_NAME_RE.fullmatch(intent.tool):
        raise ValueError("invalid tool name")
    raw_mode = intent.mode.value if isinstance(intent.mode, RunMode) else intent.mode
    if not isinstance(raw_mode, str):
        raise ValueError("invalid mode")
    if not isinstance(intent.arguments, Mapping):
        raise ValueError("arguments must be an object")
    arguments = _normalise_json_value(intent.arguments)
    data = json.dumps(
        {"arguments": arguments, "mode": raw_mode, "tool": intent.tool},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(data) > MAX_ARGUMENT_BYTES:
        raise ValueError("canonical action intent is too large")
    return data, hashlib.sha256(data).hexdigest()


def _approval_payload(approval: ActionApproval) -> bytes:
    return json.dumps(
        {
            "approved_by": approval.approved_by,
            "expires_at_s": approval.expires_at_s,
            "intent_sha256": approval.intent_sha256,
            "issued_at_s": approval.issued_at_s,
            "nonce": approval.nonce,
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_approval_key(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("approval key must contain at least 32 bytes")


def issue_action_approval(
    intent: ActionIntent,
    *,
    approved_by: str,
    issued_at_s: float,
    expires_at_s: float,
    nonce: str,
    secret: bytes,
) -> ActionApproval:
    """Issue a short-lived HMAC approval for a trusted control-plane caller.

    This function authenticates the approval record; the trusted caller remains
    responsible for showing and validating the native tool's parameter meaning.
    The signing key must never be exposed to the model or placed in a bundle.
    """

    _validate_approval_key(secret)
    if not APPROVER_RE.fullmatch(approved_by):
        raise ValueError("invalid approver id")
    if not NONCE_RE.fullmatch(nonce):
        raise ValueError("invalid approval nonce")
    if not _is_finite_number(issued_at_s) or not _is_finite_number(expires_at_s):
        raise ValueError("approval timestamps must be finite")
    lifetime = float(expires_at_s) - float(issued_at_s)
    if lifetime <= 0.0 or lifetime > MAX_APPROVAL_LIFETIME_SECONDS:
        raise ValueError("invalid approval lifetime")
    _, intent_digest = _canonical_intent(intent)
    unsigned = ActionApproval(
        intent_sha256=intent_digest,
        approved_by=approved_by,
        issued_at_s=float(issued_at_s),
        expires_at_s=float(expires_at_s),
        nonce=nonce,
        signature="",
    )
    signature = hmac.new(
        secret, _APPROVAL_DOMAIN + _approval_payload(unsigned), hashlib.sha256
    ).hexdigest()
    return ActionApproval(
        intent_sha256=unsigned.intent_sha256,
        approved_by=unsigned.approved_by,
        issued_at_s=unsigned.issued_at_s,
        expires_at_s=unsigned.expires_at_s,
        nonce=unsigned.nonce,
        signature=signature,
    )


def _approval_reasons(
    approval: ActionApproval | None,
    *,
    intent_sha256: str,
    now_s: float,
    secret: bytes | None,
    consumed_nonces: AbstractSet[str] | None,
) -> list[str]:
    if approval is None:
        return ["action_approval_required"]
    reasons: list[str] = []
    if secret is None:
        reasons.append("approval_verifier_unavailable")
    else:
        try:
            _validate_approval_key(secret)
        except ValueError:
            reasons.append("approval_verifier_invalid")
    if consumed_nonces is None:
        reasons.append("approval_replay_store_unavailable")
    if not isinstance(approval.intent_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", approval.intent_sha256
    ):
        reasons.append("approval_invalid")
    elif not hmac.compare_digest(approval.intent_sha256, intent_sha256):
        reasons.append("approval_intent_mismatch")
    if not isinstance(approval.approved_by, str) or not APPROVER_RE.fullmatch(
        approval.approved_by
    ):
        reasons.append("approval_invalid")
    if not isinstance(approval.nonce, str) or not NONCE_RE.fullmatch(approval.nonce):
        reasons.append("approval_invalid")
    elif consumed_nonces is not None and approval.nonce in consumed_nonces:
        reasons.append("approval_replayed")
    timestamps_valid = (
        _is_finite_number(now_s)
        and _is_finite_number(approval.issued_at_s)
        and _is_finite_number(approval.expires_at_s)
    )
    if not timestamps_valid:
        reasons.append("approval_time_invalid")
    else:
        lifetime = float(approval.expires_at_s) - float(approval.issued_at_s)
        if lifetime <= 0.0 or lifetime > MAX_APPROVAL_LIFETIME_SECONDS:
            reasons.append("approval_lifetime_invalid")
        if float(now_s) < float(approval.issued_at_s):
            reasons.append("approval_not_yet_valid")
        elif float(now_s) > float(approval.expires_at_s):
            reasons.append("approval_expired")
    if secret is not None and "approval_verifier_invalid" not in reasons:
        try:
            expected = hmac.new(
                secret, _APPROVAL_DOMAIN + _approval_payload(approval), hashlib.sha256
            ).hexdigest()
        except (TypeError, ValueError):
            reasons.append("approval_invalid")
        else:
            if not isinstance(approval.signature, str) or not hmac.compare_digest(
                approval.signature, expected
            ):
                reasons.append("approval_signature_invalid")
    return list(dict.fromkeys(reasons))


def _decision(
    intent: ActionIntent,
    mode: str,
    risk_rule: RiskPolicy | None,
    reasons: list[str],
    intent_sha256: str | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        allowed=not reasons,
        tool=intent.tool,
        mode=mode,
        risk=None if risk_rule is None else risk_rule.risk,
        risk_level=None if risk_rule is None else risk_rule.risk_level,
        intent_sha256=intent_sha256,
        reason_codes=("allowed",) if not reasons else tuple(reasons),
    )


def evaluate_action(
    intent: ActionIntent,
    state: RobotState,
    policy: PolicyConfig,
    *,
    now_s: float,
    approval_secret: bytes | None = None,
    consumed_approval_nonces: AbstractSet[str] | None = None,
) -> PolicyDecision:
    """Evaluate one action without I/O, mutation, robot calls, or an implicit clock.

    Every applicable gate is evaluated so a caller can log the complete denial
    reason set.  Unknown required state fails closed.  Non-physical tools remain
    available for diagnostics even when the robot cannot safely move.
    """

    raw_mode = intent.mode.value if isinstance(intent.mode, RunMode) else intent.mode
    if not isinstance(raw_mode, str):
        return _decision(intent, str(raw_mode), None, ["invalid_mode"])
    try:
        mode = RunMode(raw_mode)
    except ValueError:
        return _decision(intent, raw_mode, None, ["invalid_mode"])

    tool_rule = policy.tool_policy(intent.tool)
    if tool_rule is None:
        return _decision(intent, mode.value, None, ["tool_not_allowlisted"])
    risk_rule = policy.risk_policy(tool_rule.risk)
    if risk_rule is None:
        return _decision(intent, mode.value, None, ["risk_policy_missing"])
    try:
        _, intent_sha256 = _canonical_intent(intent)
    except (TypeError, ValueError):
        return _decision(intent, mode.value, risk_rule, ["invalid_arguments"])

    reasons: list[str] = []
    if mode not in risk_rule.allowed_modes:
        reasons.append("mode_not_allowed")
        return _decision(intent, mode.value, risk_rule, reasons, intent_sha256)
    if mode in risk_rule.supervisor_approval_modes:
        reasons.extend(
            _approval_reasons(
                intent.approval,
                intent_sha256=intent_sha256,
                now_s=now_s,
                secret=approval_secret,
                consumed_nonces=consumed_approval_nonces,
            )
        )

    if risk_rule.requires_fault_free:
        if not isinstance(state.fault, bool):
            reasons.append("fault_state_unknown")
        elif state.fault:
            reasons.append("robot_fault")

    if risk_rule.requires_idle:
        if not isinstance(state.action_busy, bool):
            reasons.append("action_busy_state_unknown")
        elif state.action_busy:
            reasons.append("action_busy")

    if not risk_rule.allow_while_charging:
        if not isinstance(state.charging, bool):
            reasons.append("charging_state_unknown")
        elif state.charging:
            reasons.append("robot_charging")

    minimum_battery = risk_rule.minimum_battery_percent
    if minimum_battery is not None:
        if not _is_finite_number(state.battery_percent):
            reasons.append("battery_state_unknown")
        else:
            battery = float(state.battery_percent)
            if battery < 0.0 or battery > 100.0:
                reasons.append("battery_state_invalid")
            elif battery < minimum_battery:
                reasons.append("battery_below_minimum")

    if risk_rule.requires_stationary:
        if not isinstance(state.stationary, bool):
            reasons.append("stationary_state_unknown")
        elif not state.stationary:
            reasons.append("robot_not_stationary")

    max_sensor_age = risk_rule.max_sensor_age_seconds
    if max_sensor_age is not None:
        if not _is_finite_number(now_s):
            reasons.append("current_time_invalid")
        elif not _is_finite_number(state.sensors_observed_at_s):
            reasons.append("sensor_timestamp_unknown")
        else:
            sensor_age = float(now_s) - float(state.sensors_observed_at_s)
            if sensor_age < 0.0:
                reasons.append("sensor_timestamp_in_future")
            elif sensor_age > max_sensor_age:
                reasons.append("sensors_stale")

    if risk_rule.requires_person_present:
        if not isinstance(state.person_present, bool):
            reasons.append("person_presence_unknown")
        elif not state.person_present:
            reasons.append("person_not_present")

    if risk_rule.physical and risk_rule.cooldown_seconds > 0.0:
        if state.physical_action_history_known is not True:
            reasons.append("cooldown_history_unknown")
        elif state.last_physical_action_at_s is not None:
            if not _is_finite_number(now_s):
                if "current_time_invalid" not in reasons:
                    reasons.append("current_time_invalid")
            elif not _is_finite_number(state.last_physical_action_at_s):
                reasons.append("cooldown_timestamp_invalid")
            else:
                elapsed = float(now_s) - float(state.last_physical_action_at_s)
                if elapsed < 0.0:
                    reasons.append("cooldown_timestamp_in_future")
                elif elapsed < risk_rule.cooldown_seconds:
                    reasons.append("cooldown_active")

    return _decision(intent, mode.value, risk_rule, reasons, intent_sha256)

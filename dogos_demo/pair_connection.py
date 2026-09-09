"""Readiness gate for two independently routed Vbot connections."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from typing_extensions import assert_never

from dogos_demo.connection_models import (
    EndpointConnectionReport,
    EndpointConnectionState,
    PairConnectionConfig,
    PairConnectionReport,
    PairConnectionState,
)
from dogos_demo.rosbridge_identity import IdentityProbe, VbotIdentityProbe


def _normalize_verified_identity(report: EndpointConnectionReport) -> EndpointConnectionReport:
    match report.state:
        case EndpointConnectionState.CONNECTED:
            mismatched = (
                report.observed_robot_id is not None
                and report.observed_robot_id != report.expected_robot_id
            )
            return (
                report.model_copy(
                    update={
                        "state": EndpointConnectionState.IDENTITY_MISMATCH,
                        "detail": "observed hardware ID does not match the fixed route",
                    }
                )
                if mismatched
                else report
            )
        case (
            EndpointConnectionState.UNREACHABLE
            | EndpointConnectionState.PROTOCOL_ERROR
            | EndpointConnectionState.IDENTITY_MISMATCH
        ):
            return report
        case unreachable:
            assert_never(unreachable)


def _trusted_robot_id(report: EndpointConnectionReport) -> str | None:
    match report.state:
        case EndpointConnectionState.CONNECTED | EndpointConnectionState.IDENTITY_MISMATCH:
            return report.observed_robot_id
        case EndpointConnectionState.UNREACHABLE | EndpointConnectionState.PROTOCOL_ERROR:
            return None
        case unreachable:
            assert_never(unreachable)


def check_pair_connections(
    config: PairConnectionConfig,
    probe: IdentityProbe | None = None,
) -> PairConnectionReport:
    """Probe both fixed routes and refuse to claim readiness on partial connectivity."""
    selected_probe = VbotIdentityProbe() if probe is None else probe
    with ThreadPoolExecutor(
        max_workers=len(config.dogs),
        thread_name_prefix="dogos-connect",
    ) as pool:
        futures = tuple(
            pool.submit(selected_probe.probe, endpoint, config.timeout_ms)
            for endpoint in config.dogs
        )
        reports = tuple(future.result() for future in futures)
    normalized = tuple(_normalize_verified_identity(report) for report in reports)
    first_id, second_id = (_trusted_robot_id(report) for report in normalized)
    duplicate_device = first_id is not None and first_id == second_id
    routing_conflict = any(
        report.state == EndpointConnectionState.IDENTITY_MISMATCH for report in normalized
    )
    both_connected = all(report.state == EndpointConnectionState.CONNECTED for report in normalized)
    if duplicate_device:
        pair_state = PairConnectionState.DUPLICATE_DEVICE
    elif routing_conflict:
        pair_state = PairConnectionState.ROUTING_CONFLICT
    else:
        pair_state = PairConnectionState.READY if both_connected else PairConnectionState.DEGRADED
    return PairConnectionReport(
        state=pair_state,
        dogs=(normalized[0], normalized[1]),
    )

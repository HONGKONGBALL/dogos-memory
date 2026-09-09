"""Unit checks for the fixed two-Vbot readiness gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import Final

import pytest
from pydantic import ValidationError

from dogos_demo.connection_models import (
    EndpointConnectionReport,
    EndpointConnectionState,
    PairConnectionConfig,
    PairConnectionState,
    RobotPn,
    VbotEndpoint,
)
from dogos_demo.pair_connection import check_pair_connections
from dogos_demo.vbot_identity_core import RobotHardwareId

EXAMPLE_CONFIG: Final = Path(__file__).parents[1] / "examples" / "two_vbots.connections.json"


def pair_config() -> PairConnectionConfig:
    return PairConnectionConfig.model_validate(
        {
            "timeout_ms": 100,
            "dogs": [
                {
                    "dog_id": "dog_a",
                    "adapter_id": "adapter-a",
                    "url": "ws://127.0.0.1:19091",
                    "expected_robot_id": "vbot-aaaaaaaaaaaaaaaa",
                },
                {
                    "dog_id": "dog_b",
                    "adapter_id": "adapter-b",
                    "url": "ws://127.0.0.1:19092",
                    "expected_robot_id": "vbot-bbbbbbbbbbbbbbbb",
                },
            ],
        }
    )


def test_packaged_pair_config_uses_the_two_fixed_demo_routes() -> None:
    # Given: the connection template shipped with the hackathon package.
    raw = EXAMPLE_CONFIG.read_text(encoding="utf-8")

    # When: it crosses the same boundary as the CLI.
    config = PairConnectionConfig.model_validate_json(raw)

    # Then: A and B have distinct logical adapters and local tunnel ports.
    assert [dog.dog_id for dog in config.dogs] == ["dog_a", "dog_b"]
    assert [dog.adapter_id for dog in config.dogs] == ["adapter-a", "adapter-b"]
    assert [dog.url.port for dog in config.dogs] == [19091, 19092]


def connected(endpoint: VbotEndpoint, observed_id: str) -> EndpointConnectionReport:
    return EndpointConnectionReport(
        dog_id=endpoint.dog_id,
        adapter_id=endpoint.adapter_id,
        url=endpoint.url,
        state=EndpointConnectionState.CONNECTED,
        expected_robot_id=endpoint.expected_robot_id,
        observed_robot_id=RobotHardwareId(observed_id),
        observed_robot_pn=RobotPn("0000000000000000000"),
        identity_source="device-tree-serial+eth2-mac-sha256",
        detail="read-only identity verified",
    )


def identity_mismatch(endpoint: VbotEndpoint, observed_id: str) -> EndpointConnectionReport:
    return EndpointConnectionReport(
        dog_id=endpoint.dog_id,
        adapter_id=endpoint.adapter_id,
        url=endpoint.url,
        state=EndpointConnectionState.IDENTITY_MISMATCH,
        expected_robot_id=endpoint.expected_robot_id,
        observed_robot_id=RobotHardwareId(observed_id),
        observed_robot_pn=RobotPn("0000000000000000000"),
        identity_source="device-tree-serial+eth2-mac-sha256",
        detail="observed hardware ID does not match the fixed route",
    )


def test_connected_report_requires_the_expected_physical_hardware_id() -> None:
    # Given: a reachable route without validated physical identity evidence.
    endpoint = pair_config().dogs[0]

    # When/Then: it cannot be represented as connected.
    with pytest.raises(ValidationError, match="endpoint_connection_contract"):
        _ = EndpointConnectionReport(
            dog_id=endpoint.dog_id,
            adapter_id=endpoint.adapter_id,
            url=endpoint.url,
            state=EndpointConnectionState.CONNECTED,
            expected_robot_id=endpoint.expected_robot_id,
            detail="missing hardware identity",
        )


@dataclass(frozen=True, slots=True)
class FakeIdentityProbe:
    """In-memory probe preserving complete connection-report behavior."""

    reports: tuple[EndpointConnectionReport, EndpointConnectionReport]

    def probe(self, endpoint: VbotEndpoint, timeout_ms: int) -> EndpointConnectionReport:
        del timeout_ms
        return next(report for report in self.reports if report.dog_id == endpoint.dog_id)


@dataclass(frozen=True, slots=True)
class SynchronizingIdentityProbe:
    """Probe that succeeds only when both routes are checked concurrently."""

    reports: tuple[EndpointConnectionReport, EndpointConnectionReport]
    barrier: Barrier

    def probe(self, endpoint: VbotEndpoint, timeout_ms: int) -> EndpointConnectionReport:
        _ = self.barrier.wait(timeout=timeout_ms / 1_000)
        return next(report for report in self.reports if report.dog_id == endpoint.dog_id)


def test_pair_is_ready_when_both_routes_match_distinct_ids_despite_zero_pns() -> None:
    # Given: both endpoints report their configured physical identities.
    config = pair_config()
    probe = FakeIdentityProbe(
        reports=(
            connected(config.dogs[0], "vbot-aaaaaaaaaaaaaaaa"),
            connected(config.dogs[1], "vbot-bbbbbbbbbbbbbbbb"),
        )
    )

    # When: the central coordinator checks the pair.
    report = check_pair_connections(config, probe)

    # Then: encounters may proceed through the two fixed routes.
    assert report.state == PairConnectionState.READY


def test_pair_probes_both_fixed_routes_concurrently() -> None:
    # Given: each route waits until the other route has begun probing.
    config = pair_config()
    probe = SynchronizingIdentityProbe(
        reports=(
            connected(config.dogs[0], "vbot-aaaaaaaaaaaaaaaa"),
            connected(config.dogs[1], "vbot-bbbbbbbbbbbbbbbb"),
        ),
        barrier=Barrier(2),
    )

    # When: the pair checker runs one readiness cycle.
    report = check_pair_connections(config, probe)

    # Then: both checks complete in the same bounded cycle.
    assert report.state == PairConnectionState.READY


def test_pair_rejects_two_tunnels_that_reach_the_same_physical_robot() -> None:
    # Given: both local ports accidentally forward to dog A.
    config = pair_config()
    probe = FakeIdentityProbe(
        reports=(
            connected(config.dogs[0], "vbot-aaaaaaaaaaaaaaaa"),
            identity_mismatch(config.dogs[1], "vbot-aaaaaaaaaaaaaaaa"),
        )
    )

    # When: the pair is checked before an encounter.
    report = check_pair_connections(config, probe)

    # Then: the duplicate body is a hard connection failure.
    assert report.state == PairConnectionState.DUPLICATE_DEVICE


def test_pair_rejects_swapped_physical_robot_routes() -> None:
    # Given: A's tunnel reaches B and B's tunnel reaches A.
    config = pair_config()
    probe = FakeIdentityProbe(
        reports=(
            identity_mismatch(config.dogs[0], "vbot-bbbbbbbbbbbbbbbb"),
            identity_mismatch(config.dogs[1], "vbot-aaaaaaaaaaaaaaaa"),
        )
    )

    # When: the pair is checked before an encounter.
    report = check_pair_connections(config, probe)

    # Then: routing is rejected instead of silently relabelling the dogs.
    assert report.state == PairConnectionState.ROUTING_CONFLICT


def test_pair_does_not_trust_an_identity_from_a_rejected_protocol_response() -> None:
    # Given: A's response is malformed even though it contains another hardware ID.
    config = pair_config()
    invalid_a = EndpointConnectionReport(
        dog_id=config.dogs[0].dog_id,
        adapter_id=config.dogs[0].adapter_id,
        url=config.dogs[0].url,
        state=EndpointConnectionState.PROTOCOL_ERROR,
        expected_robot_id=config.dogs[0].expected_robot_id,
        observed_robot_id=RobotHardwareId("vbot-cccccccccccccccc"),
        observed_robot_pn=RobotPn("0000000000000000000"),
        detail="service rejected response",
    )
    probe = FakeIdentityProbe(
        reports=(invalid_a, connected(config.dogs[1], "vbot-bbbbbbbbbbbbbbbb"))
    )

    # When: the pair checker classifies the cycle.
    report = check_pair_connections(config, probe)

    # Then: untrusted identity data cannot be promoted into a routing diagnosis.
    assert report.state == PairConnectionState.DEGRADED


@pytest.mark.parametrize("conflict", ["dog_id", "adapter_id", "url", "robot_id"])
def test_connection_config_rejects_every_ambiguous_pair_binding(conflict: str) -> None:
    # Given: one dimension of the second fixed route aliases the first.
    valid = pair_config()
    first, second = valid.dogs
    updates = {
        "dog_id": {"dog_id": first.dog_id},
        "adapter_id": {"adapter_id": first.adapter_id},
        "url": {"url": first.url},
        "robot_id": {"expected_robot_id": first.expected_robot_id},
    }
    conflicting_second = second.model_copy(update=updates[conflict])

    # When/Then: the unambiguous two-body contract rejects the configuration.
    with pytest.raises(ValidationError, match="pair_connection_contract"):
        _ = PairConnectionConfig(
            timeout_ms=valid.timeout_ms,
            dogs=(first, conflicting_second),
        )

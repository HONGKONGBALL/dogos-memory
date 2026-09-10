"""Verify that a Vbot address is reached on a directly attached local subnet."""

from __future__ import annotations

import argparse
import ipaddress
import json
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class LinkReport:
    ok: bool
    host: str
    interface: str | None
    local_address: str | None
    gateway: str | None
    blocker: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "host": self.host,
            "interface": self.interface,
            "local_address": self.local_address,
            "gateway": self.gateway,
            "blocker": self.blocker,
        }


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(name)}:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def evaluate_darwin_route(host: str, route_text: str, ifconfig_text: str) -> LinkReport:
    address = ipaddress.ip_address(host)
    if not isinstance(address, ipaddress.IPv4Address):
        return LinkReport(False, host, None, None, None, "ipv4_required")
    interface = _field(route_text, "interface")
    gateway = _field(route_text, "gateway")
    if interface is None:
        return LinkReport(False, host, None, None, gateway, "route_interface_missing")
    local_candidates = re.findall(r"^\s*inet\s+(\d+\.\d+\.\d+\.\d+)\b", ifconfig_text, re.MULTILINE)
    local: str | None = None
    host_network = ipaddress.ip_network(f"{host}/24", strict=False)
    for candidate in local_candidates:
        if ipaddress.ip_address(candidate) in host_network:
            local = candidate
            break
    if local is None:
        return LinkReport(False, host, interface, None, gateway, "no_same_subnet_address")
    if gateway and not gateway.startswith("link#"):
        try:
            gateway_address = ipaddress.ip_address(gateway)
        except ValueError:
            return LinkReport(False, host, interface, local, gateway, "unexpected_gateway")
        if gateway_address not in host_network:
            return LinkReport(False, host, interface, local, gateway, "route_uses_other_gateway")
    return LinkReport(True, host, interface, local, gateway, None)


def check_link(host: str) -> LinkReport:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return LinkReport(False, host, None, None, None, "invalid_host")
    if not isinstance(address, ipaddress.IPv4Address):
        return LinkReport(False, host, None, None, None, "ipv4_required")
    if platform.system() != "Darwin":
        return LinkReport(False, host, None, None, None, "unsupported_host_os")
    try:
        route = subprocess.run(
            ["/sbin/route", "-n", "get", host],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return LinkReport(False, host, None, None, None, "route_query_failed")
    if route.returncode != 0:
        return LinkReport(False, host, None, None, None, "route_missing")
    interface = _field(route.stdout, "interface")
    if interface is None or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,32}", interface):
        return LinkReport(False, host, interface, None, _field(route.stdout, "gateway"), "route_interface_missing")
    try:
        interface_state = subprocess.run(
            ["/sbin/ifconfig", interface],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return LinkReport(False, host, interface, None, _field(route.stdout, "gateway"), "interface_query_failed")
    if interface_state.returncode != 0 or "status: inactive" in interface_state.stdout:
        return LinkReport(False, host, interface, None, _field(route.stdout, "gateway"), "interface_inactive")
    return evaluate_darwin_route(host, route.stdout, interface_state.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m native_agent.runtime.link_check")
    parser.add_argument("--host", required=True)
    args = parser.parse_args(argv)
    report = check_link(args.host)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

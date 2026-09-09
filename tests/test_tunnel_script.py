"""CLI contract for the persistent per-Vbot SSH tunnel launcher."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

PROJECT: Final = Path(__file__).parents[1]
SCRIPT: Final = PROJECT / "scripts" / "connect-vbot-tunnel.sh"


def test_tunnel_launcher_documents_fixed_route_and_reconnect_usage() -> None:
    # Given: the packaged tunnel launcher, without contacting a robot.
    # When: its local help path is invoked.
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "--help"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    # Then: the operator sees the two required route inputs and retry behavior.
    assert result.returncode == 0, result.stderr
    assert "--host" in result.stdout
    assert "--local-port" in result.stdout
    assert "reconnect" in result.stdout.lower()


def test_tunnel_dry_run_pins_one_local_port_to_identity_bridge(tmp_path: Path) -> None:
    # Given: an explicit SSH alias file and dog A's fixed local port.
    ssh_config = tmp_path / "ssh_config"
    _ = ssh_config.write_text("Host vbot-a\n  HostName 192.0.2.10\n", encoding="utf-8")

    # When: the operator previews the connection without opening a network socket.
    result = subprocess.run(
        [
            "/bin/bash",
            str(SCRIPT),
            "--dry-run",
            "--ssh-config",
            str(ssh_config),
            "--host",
            "vbot-a",
            "--local-port",
            "19091",
        ],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    # Then: the preview contains only identity startup and its fixed local forward.
    assert result.returncode == 0, result.stderr
    assert "/userdata/vbot/dogos-identity/current/vbot_identity_bridge.py" in result.stdout
    assert "127.0.0.1:19091:127.0.0.1:9092" in result.stdout
    assert "/userdata/vbot/agenticros-adapter/bridge.py" not in result.stdout
    assert "/sm/action/lowlevel" not in result.stdout


def test_tunnel_launcher_starts_bridge_then_opens_one_fixed_forward(tmp_path: Path) -> None:
    # Given: a local fake at the SSH process boundary and one bounded attempt.
    ssh_config = tmp_path / "ssh_config"
    _ = ssh_config.write_text("Host vbot-a\n  HostName 192.0.2.10\n", encoding="utf-8")
    call_log = tmp_path / "ssh-calls.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    _ = fake_ssh.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$FAKE_SSH_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o700)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["FAKE_SSH_LOG"] = str(call_log)

    # When: the launcher performs one real control-flow attempt.
    result = subprocess.run(
        [
            "/bin/bash",
            str(SCRIPT),
            "--ssh-config",
            str(ssh_config),
            "--host",
            "vbot-a",
            "--local-port",
            "19091",
            "--max-attempts",
            "1",
        ],
        cwd=PROJECT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    # Then: identity startup precedes one keepalive tunnel and no control bridge is touched.
    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert "bash -s" in calls[0]
    assert "-N -L 127.0.0.1:19091:127.0.0.1:9092 vbot-a" in calls[1]
    assert all("/sm/action/lowlevel" not in call for call in calls)


def test_tunnel_launcher_disables_nounset_while_loading_vendor_ros(tmp_path: Path) -> None:
    # Given: a fake SSH boundary that captures the remote startup program.
    ssh_config = tmp_path / "ssh_config"
    _ = ssh_config.write_text("Host vbot-a\n  HostName 192.0.2.10\n", encoding="utf-8")
    remote_program = tmp_path / "remote-program.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    _ = fake_ssh.write_text(
        """#!/usr/bin/env bash
if [[ "$*" == *"bash -s"* ]]; then
  /bin/cat > "$FAKE_REMOTE_PROGRAM"
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o700)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["FAKE_REMOTE_PROGRAM"] = str(remote_program)

    # When: one startup attempt sends its program to the Vbot shell.
    result = subprocess.run(
        [
            "/bin/bash",
            str(SCRIPT),
            "--ssh-config",
            str(ssh_config),
            "--host",
            "vbot-a",
            "--local-port",
            "19091",
            "--max-attempts",
            "1",
        ],
        cwd=PROJECT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    # Then: vendor setup runs with nounset disabled and strict mode resumes afterward.
    assert result.returncode == 0, result.stderr
    source = remote_program.read_text(encoding="utf-8")
    disable_index = source.index("set +u")
    setup_index = source.index("source /app/opt/ros/humble/setup.bash")
    restore_index = source.index("set -u", setup_index)
    assert disable_index < setup_index < restore_index

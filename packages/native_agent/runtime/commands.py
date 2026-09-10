"""Construct the only X5 shell strings permitted to the trusted administrator."""

from __future__ import annotations

import re
import shlex

from native_agent.identity.bundle import DOG_ID_RE, REVISION_RE, SHA256_RE


_CAPSULE_PATH_RE = re.compile(r"^/app_param/vbot-native-installer-[0-9a-f]{16}\.py$")
_ENVELOPE_PATH_RE = re.compile(
    r"^/app_param/dogos-import-[a-z0-9_-]{1,64}-"
    r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\.json$"
)


def _validate_parts(
    capsule_path: str,
    capsule_sha256: str,
    dog_id: str,
    revision: str,
) -> None:
    if not _CAPSULE_PATH_RE.fullmatch(capsule_path):
        raise ValueError("invalid capsule path")
    if not SHA256_RE.fullmatch(capsule_sha256):
        raise ValueError("invalid capsule digest")
    if not DOG_ID_RE.fullmatch(dog_id):
        raise ValueError("invalid dog id")
    if not REVISION_RE.fullmatch(revision):
        raise ValueError("invalid revision")


def build_x5_apply_command(
    *,
    capsule_path: str,
    capsule_sha256: str,
    envelope_path: str,
    dog_id: str,
    revision: str,
) -> str:
    _validate_parts(capsule_path, capsule_sha256, dog_id, revision)
    if not _ENVELOPE_PATH_RE.fullmatch(envelope_path):
        raise ValueError("invalid envelope path")
    quoted = [
        shlex.quote(value)
        for value in (capsule_path, capsule_sha256, envelope_path, dog_id, revision)
    ]
    capsule, digest, envelope, dog, release = quoted
    return (
        "set -eu; umask 077; "
        f"test \"$(sha256sum {capsule} | cut -d ' ' -f 1)\" = {digest}; "
        f"exec python3 {capsule} apply --envelope {envelope} "
        f"--expected-dog-id {dog} --expected-revision {release} "
        f"--execute-live-confirmation {dog}"
    )


def build_x5_rollback_command(
    *, capsule_path: str, capsule_sha256: str, dog_id: str, revision: str
) -> str:
    _validate_parts(capsule_path, capsule_sha256, dog_id, revision)
    capsule, digest, dog, release = (
        shlex.quote(value)
        for value in (capsule_path, capsule_sha256, dog_id, revision)
    )
    return (
        "set -eu; umask 077; "
        f"test \"$(sha256sum {capsule} | cut -d ' ' -f 1)\" = {digest}; "
        f"exec python3 {capsule} rollback --revision {release} "
        f"--expected-dog-id {dog} --execute-live-confirmation {dog}"
    )


def build_x5_cleanup_command(*paths: str) -> str:
    if not paths or any(
        not (_CAPSULE_PATH_RE.fullmatch(path) or _ENVELOPE_PATH_RE.fullmatch(path))
        for path in paths
    ):
        raise ValueError("cleanup accepts only generated staging paths")
    return "rm -f -- " + " ".join(shlex.quote(path) for path in paths)


X5_READ_ONLY_PROBE_COMMAND = r'''python3 -c 'import json,os,pathlib,shutil,subprocess
p=pathlib.Path("/userdata/.vbot-agent/import-state.json")
try:
 s=json.loads(p.read_text()) if p.is_file() and not p.is_symlink() and p.stat().st_size<=4096 else None
except Exception:
 s={"invalid":True}
c=[]
for q in pathlib.Path("/proc").glob("[0-9]*/cmdline"):
 try:
  if int(q.parent.name) in (os.getpid(),os.getppid()): continue
  args=q.read_bytes().split(bytes([0]))
  paths=[v.decode("utf-8","replace") for v in args if v and not any(b in v for b in (b" ",b"\n",b"\t"))]
  if any(v=="companion/server.py" or v=="companion.server" or "/companion/" in v for v in paths): c.append(q.parent.name)
 except Exception: pass
r={"probe_success":True,"harness_active":subprocess.run(["systemctl","is-active","--quiet","vbot-agent-harness.service"],check=False).returncode==0,"free_bytes":shutil.disk_usage("/userdata").free,"memory_dir_ready":pathlib.Path("/userdata/.vbot-agent/memory").is_dir(),"agents_parent_ready":pathlib.Path("/app/vbot-agent-harness").is_dir(),"identity_state":s,"competing_agents":sorted(c),"policy_hook_enforced":False}
print(json.dumps(r,separators=(",",":"),sort_keys=True))' '''.strip()

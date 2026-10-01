"""Root-gated proof that the per-notebook uid jail actually holds.

These tests do not run in default CI and are not expected to. The
``pytest-notebook-host`` job runs on an unprivileged GitHub runner, and the
root ``addopts`` in ``pyproject.toml`` already deselect the ``slow`` marker
every test in this module carries. Dropping into another uid via
``os.setuid`` requires ``CAP_SETUID``, which an unprivileged process never
has — so unlike the rest of the suite, these cannot be faked with a
monkeypatched ``preexec_fn``; they exist specifically to prove the property
the monkeypatched tests elsewhere in this tree cannot.

Run them explicitly, as root:

    sudo -E uv run pytest apps/notebook-host/tests/test_jail_privilege.py -m slow

or inside a container built from ``apps/notebook-host/Dockerfile``, which
runs as root by default.

An automated home for these belongs with the already-anticipated
notebook-host ``slow`` tier on a Linux runner (tracked as a future
requirement) — this plan does not add that CI job.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from notebook_host.blogs_store import BlogRecord, save_blogs
from notebook_host.jail import (
    DATA_DIR_MODE,
    build_jailed_preexec,
    ensure_slug_jail,
    lock_data_dir_root,
    save_uid_registry,
)
from notebook_host.lifecycle import scrub_env
from notebook_host.pids_store import save_pids

_UID_A = 100001
_UID_B = 100002


def _run_as_uid(
    uid: int, code: str, *, cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a child process dropped into ``uid`` via preexec_fn."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        preexec_fn=build_jailed_preexec(uid, rlimit_as_bytes=None, rlimit_cpu_seconds=None),
        capture_output=True,
        text=True,
        timeout=30,
    )


_READ_CODE = """
import json
try:
    with open({path!r}) as f:
        data = f.read()
except PermissionError:
    print(json.dumps({{"error": "PermissionError"}}))
except OSError as e:
    print(json.dumps({{"error": "OSError:" + type(e).__name__}}))
else:
    print(json.dumps({{"data": data}}))
"""


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_child_drops_to_uid_gid_and_clears_supplementary_groups(tmp_path: Path) -> None:
    """The child reports getuid()==uid, getgid()==uid, and an empty supplementary group list.

    The groups assertion is the one that catches a missing or misordered
    ``setgroups([])`` — without it the child keeps 0 (root) in its
    supplementary list even after setuid/setgid drop the primary identity.
    """
    paths = ensure_slug_jail(tmp_path, "slug-a", uid=_UID_A)
    code = "import json, os; print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'groups': os.getgroups()}))"
    result = _run_as_uid(_UID_A, code, cwd=str(paths.workspace))
    assert result.returncode == 0, f"child should exit cleanly; stderr={result.stderr}"
    identity = json.loads(result.stdout)
    assert identity["uid"] == _UID_A, "child should report the dropped uid"
    assert identity["gid"] == _UID_A, (
        "child should report the dropped gid (== uid, no separate allocation)"
    )
    assert identity["groups"] == [], (
        "supplementary groups must be empty — a missing setgroups([]) would leave root's groups intact"
    )


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_own_subtree_reachable_via_relative_path(tmp_path: Path) -> None:
    """With cwd inside its own workspace, a jailed process can read its own data/ via a relative path.

    ``data_dir`` (``tmp_path``) is locked to the real production mode
    (``DATA_DIR_MODE`` / 0711) here, not left at whatever the pytest fixture
    happens to default to — this proves reachability against the actual
    constraint a dropped uid faces in production, not an accidental one.
    """
    paths = ensure_slug_jail(tmp_path, "slug-a", uid=_UID_A)
    lock_data_dir_root(tmp_path)
    seeded = paths.data / "x.txt"
    seeded.write_bytes(b"hello-from-a")
    os.chown(seeded, _UID_A, _UID_A)
    os.chmod(seeded, 0o600)

    code = _READ_CODE.format(path="../data/x.txt")
    result = _run_as_uid(_UID_A, code, cwd=str(paths.workspace))
    assert result.returncode == 0, f"child should exit cleanly; stderr={result.stderr}"
    body = json.loads(result.stdout)
    assert body.get("data") == "hello-from-a", (
        f"own-subtree read should succeed and return the seeded bytes, got {body}"
    )


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_own_subtree_reachable_via_absolute_path(tmp_path: Path) -> None:
    """A jailed process can reach its own subtree by ABSOLUTE path, under the real data_dir mode.

    This is the exact property plan 05's live rehearsal found broken: under
    the old (incorrect) ``data_dir`` mode of 0700, resolving an absolute path
    requires the ``x`` bit on every component, so a dropped uid could not
    even reach its OWN ``HOME`` — every non-break-glass ``uv run`` spawn
    failed. Testing only cross-slug denial (as this module did before) let
    that unusable jail look correct; this test defends the other half.
    """
    paths = ensure_slug_jail(tmp_path, "slug-a", uid=_UID_A)
    lock_data_dir_root(tmp_path)
    seeded = paths.home / "x.txt"
    seeded.write_bytes(b"hello-from-a-home")
    os.chown(seeded, _UID_A, _UID_A)
    os.chmod(seeded, 0o600)

    code = _READ_CODE.format(path=str(seeded))
    result = _run_as_uid(_UID_A, code)  # no cwd inside the tree — pure absolute resolution
    assert result.returncode == 0, f"child should exit cleanly; stderr={result.stderr}"
    body = json.loads(result.stdout)
    assert body.get("data") == "hello-from-a-home", (
        f"own-subtree absolute-path read should succeed and return the seeded bytes, got {body}"
    )


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_cross_slug_relative_escape_denied(tmp_path: Path) -> None:
    """A relative '..' traversal into a sibling slug's data is denied.

    ``data_dir`` is locked to the real production mode (0711, traversable)
    here, not the old incorrect 0700 — this proves the denial comes from
    ``slug-b``'s own 0700 boundary, which is the actual isolation mechanism,
    not from an accidentally-untraversable parent.
    """
    paths_a = ensure_slug_jail(tmp_path, "slug-a", uid=_UID_A)
    paths_b = ensure_slug_jail(tmp_path, "slug-b", uid=_UID_B)
    secret = paths_b.data / "secret.txt"
    secret.write_bytes(b"top-secret")
    os.chown(secret, _UID_B, _UID_B)
    os.chmod(secret, 0o600)
    os.chown(tmp_path, 0, 0)
    lock_data_dir_root(tmp_path)

    code = _READ_CODE.format(path="../../slug-b/data/secret.txt")
    result = _run_as_uid(_UID_A, code, cwd=str(paths_a.workspace))
    assert result.returncode == 0, f"child should exit cleanly; stderr={result.stderr}"
    body = json.loads(result.stdout)
    assert body.get("error") == "PermissionError", (
        f"a relative cross-slug escape must raise PermissionError, got {body}"
    )


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_cross_slug_absolute_escape_denied(tmp_path: Path) -> None:
    """An absolute-path read into a sibling slug's data is denied — same wall as the relative case.

    ``data_dir`` is locked to the real production mode (0711, traversable) —
    an absolute path CAN resolve through it, but ``slug-b``'s own 0700
    boundary still denies the read. This is the case plan 05's rehearsal
    proved broken under the old 0700 parent mode (nothing could resolve
    ANY absolute path through it, own subtree included); this test proves
    the corrected mode still holds the cross-slug wall.
    """
    paths_a = ensure_slug_jail(tmp_path, "slug-a", uid=_UID_A)
    paths_b = ensure_slug_jail(tmp_path, "slug-b", uid=_UID_B)
    secret = paths_b.data / "secret.txt"
    secret.write_bytes(b"top-secret")
    os.chown(secret, _UID_B, _UID_B)
    os.chmod(secret, 0o600)
    os.chown(tmp_path, 0, 0)
    lock_data_dir_root(tmp_path)

    code = _READ_CODE.format(path=str(secret))
    result = _run_as_uid(_UID_A, code, cwd=str(paths_a.workspace))
    assert result.returncode == 0, f"child should exit cleanly; stderr={result.stderr}"
    body = json.loads(result.stdout)
    assert body.get("error") == "PermissionError", (
        f"an absolute cross-slug escape must raise PermissionError, got {body}"
    )


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_registry_files_unreachable(tmp_path: Path) -> None:
    """blogs.json/pids.json/uids.json are unreadable by a jailed uid, by their OWN mode.

    ``data_dir`` is locked to the real production mode (0711, traversable) —
    the old test locked it to 0700, which made this assertion pass for the
    wrong reason (an untraversable parent, not the file's own mode). The
    files are written through the real production save_* functions rather
    than hand-authored, so this also proves those functions actually emit
    0600 on disk, not merely that a manually-chmod'ed file would be denied.
    """
    paths_a = ensure_slug_jail(tmp_path, "slug-a", uid=_UID_A)
    blogs = tmp_path / "blogs.json"
    pids = tmp_path / "pids.json"
    uids = tmp_path / "uids.json"
    save_blogs(blogs, {"slug-a": BlogRecord(slug="slug-a", created_at=1.0)})
    save_pids(pids, {})
    save_uid_registry(uids, {"slug-a": _UID_A})
    os.chown(tmp_path, 0, 0)
    lock_data_dir_root(tmp_path)

    for registry_path in (blogs, pids, uids):
        assert registry_path.stat().st_mode & 0o777 == 0o600, (
            f"{registry_path.name} must actually be written at mode 0600 by the real save path"
        )
        code = _READ_CODE.format(path=str(registry_path))
        result = _run_as_uid(_UID_A, code, cwd=str(paths_a.workspace))
        assert result.returncode == 0, f"child should exit cleanly; stderr={result.stderr}"
        body = json.loads(result.stdout)
        assert body.get("error") == "PermissionError", (
            f"{registry_path.name} must be unreadable by a jailed uid via its own mode "
            f"(data_dir is traversable at {oct(DATA_DIR_MODE)}), got {body}"
        )


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_proc_environ_closed_across_uids(tmp_path: Path) -> None:
    """A jailed uid cannot read another uid's (or root's) /proc/<pid>/environ.

    Closes scouting finding 5: same-uid env-scrub alone would not stop this;
    the uid split is what actually denies it, verified here directly rather
    than assumed.
    """
    long_lived = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        preexec_fn=build_jailed_preexec(_UID_A, rlimit_as_bytes=None, rlimit_cpu_seconds=None),
    )
    try:
        time.sleep(0.5)  # give the child a moment to actually be running
        for target_pid in (long_lived.pid, os.getpid()):
            code = _READ_CODE.format(path=f"/proc/{target_pid}/environ")
            result = _run_as_uid(_UID_B, code)
            assert result.returncode == 0, f"child should exit cleanly; stderr={result.stderr}"
            body = json.loads(result.stdout)
            assert body.get("error") == "PermissionError", (
                f"/proc/{target_pid}/environ must be unreadable by a different uid, got {body}"
            )
    finally:
        long_lived.kill()
        long_lived.wait(timeout=5)


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_uv_and_marimo_run_under_the_dropped_uid(tmp_path: Path) -> None:
    """uv run --with marimo actually works under a dropped uid, given a per-slug HOME.

    This is the single most important test in the phase — it is the thing
    that was predicted to sink the whole mechanism (CONTEXT.md D-02), and the
    thing plan 05's real-container rehearsal proved WAS broken: this test did
    not lock ``data_dir`` to its real production mode, so it validated `uv`
    against a permission state production never actually has. It now calls
    ``lock_data_dir_root`` (the exact function ``migration.py`` calls on every
    boot) before spawning, closing that gap.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not on PATH")
    paths = ensure_slug_jail(tmp_path, "slug-a", uid=_UID_A)
    lock_data_dir_root(tmp_path)
    env = scrub_env(dict(os.environ))
    env["HOME"] = str(paths.home)

    result = subprocess.run(
        [uv, "run", "--with", "marimo", "marimo", "--version"],
        cwd=str(paths.workspace),
        env=env,
        preexec_fn=build_jailed_preexec(_UID_A, rlimit_as_bytes=None, rlimit_cpu_seconds=None),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"uv run --with marimo should succeed under the dropped uid: {combined}"
    )
    assert "Permission denied" not in combined, (
        f"the dropped-uid run must not hit a permission error: {combined}"
    )


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_releasing_a_slug_kills_a_detached_survivor_before_its_uid_is_reused(
    tmp_path: Path,
) -> None:
    """A cell's `Popen(..., start_new_session=True)` escapes marimo's process group.

    Releasing the slug must kill it, or it would sit under whichever slug is
    handed the uid next and read that notebook's files.
    """
    import signal
    import subprocess
    import time

    from notebook_host.jail import build_jailed_preexec, get_or_create_slug_uid, remove_slug_tree

    uids = tmp_path / "uids.json"
    uid = get_or_create_slug_uid(uids, "slug-a", start=_UID_A, end=_UID_A + 5)
    ensure_slug_jail(tmp_path, "slug-a", uid=uid)
    survivor = subprocess.Popen(
        ["sleep", "123457"],
        preexec_fn=build_jailed_preexec(uid, rlimit_as_bytes=None, rlimit_cpu_seconds=None),
        start_new_session=True,
    )
    try:
        remove_slug_tree(tmp_path, "slug-a", uids_file=uids)
        deadline = time.monotonic() + 5
        while survivor.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert survivor.poll() is not None, "the detached survivor must be dead"
        assert survivor.returncode == -signal.SIGKILL
    finally:
        if survivor.poll() is None:
            survivor.kill()


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_jailed_child_has_no_new_privs_and_a_process_cap() -> None:
    import subprocess

    from notebook_host.jail import JAIL_RLIMIT_NPROC, build_jailed_preexec

    out = subprocess.run(
        [
            "/bin/sh",
            "-c",
            "grep NoNewPrivs /proc/self/status; grep 'Max processes' /proc/self/limits",
        ],
        preexec_fn=build_jailed_preexec(_UID_A, rlimit_as_bytes=None, rlimit_cpu_seconds=None),
        capture_output=True,
        text=True,
        check=True,
        cwd="/",
    ).stdout
    assert "NoNewPrivs:\t1" in out, "setuid binaries can't hand the notebook privilege back"
    assert out.strip().splitlines()[-1].split()[2] == str(JAIL_RLIMIT_NPROC)


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() != 0, reason="uid drop requires root")
def test_jail_uid_cannot_swap_entries_in_its_host_owned_slug_root() -> None:
    """Renaming home away and planting home -> /etc must fail for the jail uid.

    If it succeeded, the host's next chown of ``home`` would hand ``/etc`` to
    the jail uid. The data dir is in its own 0711 dir under /tmp so the jail
    uid can reach it at all (pytest's tmp_path is root-only).
    """
    import shutil
    import subprocess
    import tempfile

    from notebook_host.jail import build_jailed_preexec

    base = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        base.chmod(0o711)
        paths = ensure_slug_jail(base, "slug-a", uid=_UID_A)
        preexec = build_jailed_preexec(_UID_A, rlimit_as_bytes=None, rlimit_cpu_seconds=None)

        def as_uid(*argv: str) -> int:
            return subprocess.run(list(argv), preexec_fn=preexec, cwd="/", check=False).returncode

        assert as_uid("/bin/touch", str(paths.home / "ok")) == 0, "its own home is writable"
        assert as_uid("/bin/mv", str(paths.home), str(paths.root / "h2")) != 0
        assert as_uid("/bin/ln", "-s", "/etc", str(paths.root / "home2")) != 0
        assert as_uid("/bin/rm", "-rf", str(paths.log)) != 0 or not paths.log.exists()
        assert paths.home.is_dir() and not paths.home.is_symlink()
    finally:
        shutil.rmtree(base, ignore_errors=True)

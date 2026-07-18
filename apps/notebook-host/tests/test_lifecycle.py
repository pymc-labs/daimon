"""Tests for notebook_host.config and notebook_host.lifecycle."""

from __future__ import annotations

import subprocess
import unittest.mock
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

if TYPE_CHECKING:
    from notebook_host.lifecycle import NotebookProcess

# ─── config tests ────────────────────────────────────────────────────────────


def test_settings_raises_when_admin_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() raises ValidationError when DAIMON_NOTEBOOK__ADMIN_SECRET is unset."""
    monkeypatch.delenv("DAIMON_NOTEBOOK__ADMIN_SECRET", raising=False)
    from notebook_host.config import load_settings

    with pytest.raises(ValidationError):
        load_settings(_env_file=None)


def test_settings_loads_when_admin_secret_set() -> None:
    """Settings() succeeds and exposes admin_secret.get_secret_value() when secret is set."""
    from notebook_host.config import load_settings

    settings = load_settings(_env_file=None)
    assert settings.admin_secret.get_secret_value() == "test-secret", (
        "admin_secret should match the monkeypatched env value"
    )


def test_settings_defaults_match_documented_values() -> None:
    """Settings() default values match the documented defaults."""
    from notebook_host.config import load_settings

    settings = load_settings(_env_file=None)
    assert settings.host_port == 8001, "host_port default should be 8001"
    assert settings.marimo_port_start == 8100, "marimo_port_start default should be 8100"
    assert settings.marimo_port_end == 8160, "marimo_port_end default should be 8160"
    assert settings.subprocess_ttl_seconds == 86400, (
        "subprocess_ttl_seconds default should be 86400 (24h)"
    )
    assert settings.sweep_interval_seconds == 300, "sweep_interval_seconds default should be 300"
    assert settings.spawn_timeout_seconds == 20.0, "spawn_timeout_seconds default should be 20.0"
    assert settings.public_host == "localhost", "public_host default should be 'localhost'"
    assert settings.data_dir == Path("/data/notebooks"), (
        "data_dir default should be Path('/data/notebooks')"
    )


def test_settings_env_override_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env override DAIMON_NOTEBOOK__MARIMO_PORT_START=9000 reflects on resolved Settings."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "9000")
    from notebook_host.config import load_settings

    settings = load_settings(_env_file=None)
    assert settings.marimo_port_start == 9000, (
        "marimo_port_start should reflect the env override value"
    )


def test_resolved_blogs_file_defaults_to_data_dir_blogs_json() -> None:
    """blogs_file unset → resolved_blogs_file is data_dir / 'blogs.json'."""
    from notebook_host.config import load_settings

    settings = load_settings(_env_file=None)
    assert settings.resolved_blogs_file == settings.data_dir / "blogs.json", (
        "default blogs registry must live next to the source files on the volume"
    )


def test_resolved_blogs_file_honors_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit DAIMON_NOTEBOOK__BLOGS_FILE wins over the data_dir default."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__BLOGS_FILE", "/tmp/custom-blogs.json")
    from notebook_host.config import load_settings

    settings = load_settings(_env_file=None)
    assert settings.resolved_blogs_file == Path("/tmp/custom-blogs.json"), (
        "explicit blogs_file path must be used verbatim"
    )


# ─── lifecycle tests ─────────────────────────────────────────────────────────


def _make_fake_popen(alive: bool = True) -> subprocess.Popen[bytes]:
    """Create a spec'd Popen mock with poll configured for alive/dead state."""
    mock = unittest.mock.create_autospec(subprocess.Popen, instance=True)
    mock.poll.return_value = None if alive else 0  # type: ignore[attr-defined]
    mock.pid = 99999  # type: ignore[attr-defined]
    return mock  # type: ignore[return-value]


def _marimo_subcommand(argv: list[str]) -> str:
    """Return the marimo subcommand (``edit``/``run``) from a captured argv.

    The command is ``uv run --with marimo marimo <subcommand> ...`` — ``marimo``
    appears twice (the ``--with`` arg and the executable), so we anchor off the
    LAST occurrence; a forward ``index('marimo')`` would hit the ``--with`` arg.
    A bare ``argv.index('run')`` is also wrong here because ``uv run`` injects
    its own ``run``.
    """
    last_marimo = len(argv) - 1 - argv[::-1].index("marimo")
    return argv[last_marimo + 1]


def test_allocate_port_returns_first_free_port_when_no_processes_used() -> None:
    """allocate_port({}, 8100, 8101) returns 8100 when no ports are used."""
    from notebook_host.lifecycle import allocate_port

    result = allocate_port({}, 8100, 8101)
    assert result == 8100, "should return the first port in the range when none are used"


def test_allocate_port_returns_second_port_when_first_used() -> None:
    """allocate_port with one port used returns the other."""
    from notebook_host.lifecycle import NotebookProcess, allocate_port

    proc = _make_fake_popen()
    np = NotebookProcess(
        slug="test", port=8100, process=proc, public_host="localhost", host_port=8001
    )
    result = allocate_port({"test": np}, 8100, 8101)
    assert result == 8101, "should return the second port when first is in use"


def test_allocate_port_raises_503_when_all_ports_used() -> None:
    """allocate_port raises HTTPException(503) with 'port pool exhausted' when pool is full."""
    from notebook_host.lifecycle import NotebookProcess, allocate_port

    proc1 = _make_fake_popen()
    proc2 = _make_fake_popen()
    np1 = NotebookProcess(
        slug="a", port=8100, process=proc1, public_host="localhost", host_port=8001
    )
    np2 = NotebookProcess(
        slug="b", port=8101, process=proc2, public_host="localhost", host_port=8001
    )
    with pytest.raises(HTTPException) as exc_info:
        allocate_port({"a": np1, "b": np2}, 8100, 8101)
    assert exc_info.value.status_code == 503, "should raise 503 when port pool is exhausted"
    assert "port pool exhausted" in str(exc_info.value.detail), (
        "detail should mention 'port pool exhausted'"
    )


def test_safe_slug_returns_valid_slug_unchanged() -> None:
    """safe_slug('ok-1_x') returns 'ok-1_x'."""
    from notebook_host.lifecycle import safe_slug

    result = safe_slug("ok-1_x")
    assert result == "ok-1_x", "valid slug should pass through unchanged"


def test_safe_slug_raises_400_for_empty_string() -> None:
    """safe_slug('') raises HTTPException(400)."""
    from notebook_host.lifecycle import safe_slug

    with pytest.raises(HTTPException) as exc_info:
        safe_slug("")
    assert exc_info.value.status_code == 400, "empty slug should raise 400"


def test_safe_slug_raises_400_for_path_separator() -> None:
    """safe_slug('a/b') raises HTTPException(400)."""
    from notebook_host.lifecycle import safe_slug

    with pytest.raises(HTTPException) as exc_info:
        safe_slug("a/b")
    assert exc_info.value.status_code == 400, "slug with '/' should raise 400"


def test_safe_slug_raises_400_for_parent_traversal() -> None:
    """safe_slug('..') raises HTTPException(400)."""
    from notebook_host.lifecycle import safe_slug

    with pytest.raises(HTTPException) as exc_info:
        safe_slug("..")
    assert exc_info.value.status_code == 400, "slug '..' should raise 400"


def test_safe_slug_raises_400_for_nul_byte() -> None:
    """safe_slug('a\\x00b') raises HTTPException(400)."""
    from notebook_host.lifecycle import safe_slug

    with pytest.raises(HTTPException) as exc_info:
        safe_slug("a\x00b")
    assert exc_info.value.status_code == 400, "slug with NUL byte should raise 400"


def test_safe_slug_raises_400_for_leading_dash() -> None:
    """safe_slug('-no-token') raises 400 — argv-injection defense."""
    from notebook_host.lifecycle import safe_slug

    with pytest.raises(HTTPException) as exc_info:
        safe_slug("-no-token")
    assert exc_info.value.status_code == 400, "leading '-' should raise 400"


@pytest.mark.parametrize(
    "slug",
    [
        "a.b",  # period — would land in basename, marimo confused
        "a b",  # space
        "a\\b",  # backslash
        "a\rb",  # carriage return
        "a\tb",  # tab
        "héllo",  # non-ASCII
        "‮sluG",  # unicode right-to-left override
    ],
)
def test_safe_slug_raises_400_for_chars_outside_charset(slug: str) -> None:
    """safe_slug rejects anything outside [A-Za-z0-9_-]."""
    from notebook_host.lifecycle import safe_slug

    with pytest.raises(HTTPException) as exc_info:
        safe_slug(slug)
    assert exc_info.value.status_code == 400, (
        f"slug {slug!r} contains a char outside the allowed charset"
    )


def test_safe_slug_raises_400_for_overlong_slug() -> None:
    """safe_slug rejects slugs longer than 64 chars."""
    from notebook_host.lifecycle import safe_slug

    with pytest.raises(HTTPException) as exc_info:
        safe_slug("a" * 65)
    assert exc_info.value.status_code == 400, "65-char slug should raise 400"


def test_safe_slug_accepts_token_urlsafe_output() -> None:
    """safe_slug accepts a 22-char secrets.token_urlsafe(16) string."""
    import secrets

    from notebook_host.lifecycle import safe_slug

    # token_urlsafe(16) yields 22 chars from [A-Za-z0-9_-]
    minted = secrets.token_urlsafe(16)
    assert safe_slug(minted) == minted, "bot-minted slug must round-trip cleanly"


# ─── scrub_env ───────────────────────────────────────────────────────────────


def test_scrub_env_strips_daimon_notebook_admin_secret() -> None:
    """scrub_env drops DAIMON_NOTEBOOK__ADMIN_SECRET so notebook code can't read it."""
    from notebook_host.lifecycle import scrub_env

    parent = {
        "DAIMON_NOTEBOOK__ADMIN_SECRET": "shhhh",
        "PATH": "/usr/bin",
        "HOME": "/root",
    }
    scrubbed = scrub_env(parent)
    assert "DAIMON_NOTEBOOK__ADMIN_SECRET" not in scrubbed, (
        "admin bearer must not leak into spawned notebook process"
    )
    assert scrubbed["PATH"] == "/usr/bin", "PATH must survive so marimo can find uv"
    assert scrubbed["HOME"] == "/root", "HOME must survive for normal subprocess behavior"


def test_scrub_env_strips_all_daimon_underscore_vars() -> None:
    """scrub_env drops the entire DAIMON_* namespace (anthropic key, db creds, etc.)."""
    from notebook_host.lifecycle import scrub_env

    parent = {
        "DAIMON_ANTHROPIC__API_KEY": "sk-x",
        "DAIMON_DATABASE__URL": "postgresql://...",
        "DAIMON_DISCORD__BOT_TOKEN": "abc",
        "DAIMON_NOTEBOOK__ADMIN_SECRET": "shhhh",
        "DAIMON_NOTEBOOK__DATA_DIR": "/data",
        "OTHER": "keep",
    }
    scrubbed = scrub_env(parent)
    daimon_keys = [k for k in scrubbed if k.startswith("DAIMON_")]
    assert daimon_keys == [], f"all DAIMON_-prefixed vars must be stripped; got {daimon_keys}"
    assert scrubbed.get("OTHER") == "keep", "non-DAIMON vars must survive"


def test_notebook_process_url_returns_public_url() -> None:
    """NotebookProcess.url returns http://<public_host>:<host_port>/n/<slug>/"""
    from notebook_host.lifecycle import NotebookProcess

    proc = _make_fake_popen()
    np = NotebookProcess(
        slug="my-nb", port=8100, process=proc, public_host="example.com", host_port=8001
    )
    assert np.url == "http://example.com:8001/n/my-nb/", "url should be the public-facing proxy URL"


def test_notebook_process_internal_url_returns_localhost_url() -> None:
    """NotebookProcess.internal_url returns http://localhost:<port>/n/<slug>/"""
    from notebook_host.lifecycle import NotebookProcess

    proc = _make_fake_popen()
    np = NotebookProcess(
        slug="my-nb", port=8100, process=proc, public_host="example.com", host_port=8001
    )
    assert np.internal_url == "http://localhost:8100/n/my-nb/", (
        "internal_url should point to the subprocess directly"
    )


NOTEBOOK_TEMPLATE = '''import marimo

__generated_with = "0.23.8"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md("""# subprocess test — %s

Subprocess-per-notebook test. Slug embedded in the markdown so the WS
kernel-ready payload includes it, letting the probe confirm the right
file was loaded.
""")
    return


if __name__ == "__main__":
    app.run()
'''


@pytest.mark.slow
async def test_spawn_marimo_then_kill(tmp_path: Path) -> None:
    """spawn_marimo + wait_for_port returns True; kill makes is_alive() False."""
    import time

    from notebook_host.lifecycle import NotebookProcess, kill, spawn_marimo, wait_for_port

    slug = "test-nb"
    port = 8199
    file_path = tmp_path / f"{slug}.py"
    file_path.write_text(NOTEBOOK_TEMPLATE % slug, encoding="utf-8")

    proc = spawn_marimo(slug, file_path, port)
    np = NotebookProcess(
        slug=slug,
        port=port,
        process=proc,
        public_host="localhost",
        host_port=8001,
        started_at=time.time(),
    )

    ready = await wait_for_port(port, slug, 30.0)
    assert ready is True, "marimo subprocess should become ready within 30 seconds"

    kill(np)
    assert np.is_alive() is False, "process should not be alive after kill()"
    # Verify no zombie — returncode should be available
    rc = np.process.wait()
    assert rc is not None, "process.wait() should return a returncode (no zombie)"


def test_make_preexec_returns_none_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """_make_preexec is a no-op on macOS / non-Linux platforms."""
    from notebook_host import lifecycle

    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    assert lifecycle._make_preexec(4_000_000_000, 3600) is None, (
        "non-Linux platforms must skip setrlimit since RLIMIT_AS is unreliable on macOS"
    )


def test_make_preexec_returns_none_when_no_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """_make_preexec returns None when both limits are unset, even on Linux."""
    from notebook_host import lifecycle

    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    assert lifecycle._make_preexec(None, None) is None
    assert lifecycle._make_preexec(0, 0) is None, "zero counts as unset"


def test_make_preexec_returns_callable_on_linux_with_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Linux with limits set, returns a callable that applies setrlimit."""
    import resource

    from notebook_host import lifecycle

    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    preexec = lifecycle._make_preexec(4_000_000_000, 3600)
    assert callable(preexec), "Linux + limits should yield a callable preexec_fn"

    calls: list[tuple[int, tuple[int, int]]] = []

    def fake_setrlimit(which: int, limits: tuple[int, int]) -> None:
        calls.append((which, limits))

    monkeypatch.setattr(resource, "setrlimit", fake_setrlimit)
    preexec()  # type: ignore[operator]

    assert (resource.RLIMIT_AS, (4_000_000_000, 4_000_000_000)) in calls, (
        "RLIMIT_AS should be set to the configured byte cap"
    )
    assert (resource.RLIMIT_CPU, (3600, 3600)) in calls, (
        "RLIMIT_CPU should be set to the configured second cap"
    )


# ─── _prepare_workspace (43-02) ───────────────────────────────────────────────


def test_prepare_workspace_creates_workspace_dir(tmp_path: Path) -> None:
    """_prepare_workspace creates <slug>_workspace/ next to file_path."""
    from notebook_host.lifecycle import _prepare_workspace  # pyright: ignore[reportPrivateUsage]

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    workspace = _prepare_workspace(file_path, "myslug")
    assert workspace == tmp_path / "myslug_workspace", (
        "workspace dir should be <data_dir>/<slug>_workspace"
    )
    assert workspace.is_dir(), "workspace dir should exist and be a directory"


def test_prepare_workspace_creates_data_dir(tmp_path: Path) -> None:
    """_prepare_workspace creates <slug>.data/ if it does not exist."""
    from notebook_host.lifecycle import _prepare_workspace  # pyright: ignore[reportPrivateUsage]

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    _prepare_workspace(file_path, "myslug")
    assert (tmp_path / "myslug.data").is_dir(), (
        "<slug>.data/ should be created so attach-after-publish works"
    )


def test_prepare_workspace_leaves_existing_data_dir_untouched(tmp_path: Path) -> None:
    """_prepare_workspace does not wipe pre-existing files in <slug>.data/."""
    from notebook_host.lifecycle import _prepare_workspace  # pyright: ignore[reportPrivateUsage]

    data_dir = tmp_path / "myslug.data"
    data_dir.mkdir()
    (data_dir / "existing.csv").write_bytes(b"col,val\n1,2\n")

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    _prepare_workspace(file_path, "myslug")

    assert (data_dir / "existing.csv").read_bytes() == b"col,val\n1,2\n", (
        "pre-existing attachment must survive workspace setup (publish-after-attach)"
    )


def test_prepare_workspace_creates_data_symlink(tmp_path: Path) -> None:
    """workspace/data is a symlink to ../<slug>.data."""
    import os as _os

    from notebook_host.lifecycle import _prepare_workspace  # pyright: ignore[reportPrivateUsage]

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    workspace = _prepare_workspace(file_path, "myslug")

    data_link = workspace / "data"
    assert data_link.is_symlink(), "workspace/data should be a symlink"
    assert _os.readlink(data_link) == str(Path("..") / "myslug.data"), (
        "data symlink target should be the relative path ../<slug>.data"
    )
    # And it must resolve to the actual data dir.
    assert data_link.resolve() == (tmp_path / "myslug.data").resolve(), (
        "data symlink should resolve to <data_dir>/<slug>.data"
    )


def test_prepare_workspace_creates_source_symlink(tmp_path: Path) -> None:
    """workspace/<slug>.py is a symlink to ../<slug>.py (D2 basename invariant)."""
    import os as _os

    from notebook_host.lifecycle import _prepare_workspace  # pyright: ignore[reportPrivateUsage]

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    workspace = _prepare_workspace(file_path, "myslug")

    source_link = workspace / "myslug.py"
    assert source_link.is_symlink(), "workspace/<slug>.py should be a symlink"
    assert _os.readlink(source_link) == str(Path("..") / "myslug.py"), (
        "source symlink target should be ../<slug>.py"
    )
    assert source_link.resolve() == file_path.resolve(), (
        "source symlink must resolve to the real source file"
    )


def test_prepare_workspace_is_idempotent(tmp_path: Path) -> None:
    """Calling _prepare_workspace twice produces the same final state, no errors."""
    from notebook_host.lifecycle import _prepare_workspace  # pyright: ignore[reportPrivateUsage]

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    workspace1 = _prepare_workspace(file_path, "myslug")
    workspace2 = _prepare_workspace(file_path, "myslug")

    assert workspace1 == workspace2, "two calls should return the same workspace path"
    assert (workspace2 / "data").is_symlink(), "data symlink should still exist after re-prepare"
    assert (workspace2 / "myslug.py").is_symlink(), (
        "source symlink should still exist after re-prepare"
    )
    # And they still resolve correctly.
    assert (workspace2 / "data").resolve() == (tmp_path / "myslug.data").resolve()
    assert (workspace2 / "myslug.py").resolve() == file_path.resolve()


def test_prepare_workspace_replaces_stale_symlink(tmp_path: Path) -> None:
    """A stale symlink pointing nowhere is replaced cleanly (crash-recovery shape)."""
    from notebook_host.lifecycle import _prepare_workspace  # pyright: ignore[reportPrivateUsage]

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    workspace = tmp_path / "myslug_workspace"
    workspace.mkdir()
    # Pre-create a broken symlink at the location we'll want.
    bad_link = workspace / "data"
    bad_link.symlink_to(Path("..") / "nope-does-not-exist")

    _prepare_workspace(file_path, "myslug")

    assert (workspace / "data").is_symlink(), "data symlink must still be a symlink"
    assert (workspace / "data").resolve() == (tmp_path / "myslug.data").resolve(), (
        "stale broken symlink must be replaced with the correct target"
    )


def test_spawn_marimo_uses_workspace_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """spawn_marimo sets Popen cwd= to <data_dir>/<slug>_workspace, not data_dir."""
    from notebook_host import lifecycle

    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda _x: "/usr/bin/uv",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )

    captured: dict[str, object] = {}
    sentinel_proc = _make_fake_popen()

    def fake_popen(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel_proc

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("myslug", file_path, 8100)

    assert captured.get("cwd") == str(tmp_path / "myslug_workspace"), (
        "Popen cwd should be the per-slug workspace dir, not file_path.parent"
    )
    # marimo edit must receive file_path.name (basename), not the full path.
    cmd = captured.get("args")
    if cmd is None:
        # Popen positional arg is the cmd list (we used *args).
        # fake_popen signature didn't capture positional — re-call with shape.
        pass


def test_spawn_marimo_still_uses_basename_arg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spawn_marimo passes file_path.name (not full path) to marimo edit."""
    from notebook_host import lifecycle

    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda _x: "/usr/bin/uv",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )

    captured_args: list[object] = []
    sentinel_proc = _make_fake_popen()

    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> object:
        captured_args.extend(cmd)
        return sentinel_proc

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("myslug", file_path, 8100)

    # marimo edit <basename> — the basename appears, the full path does not.
    assert "myslug.py" in captured_args, "marimo edit arg should be the basename"
    assert str(file_path) not in captured_args, (
        "absolute path must not appear; basename + workspace cwd is the contract"
    )


def test_spawn_marimo_creates_workspace_and_data_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spawn_marimo side-effect: workspace + data dir + symlinks exist after call."""
    from notebook_host import lifecycle

    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda _x: "/usr/bin/uv",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    sentinel = _make_fake_popen()
    monkeypatch.setattr(
        lifecycle.subprocess,
        "Popen",
        lambda *a, **kw: sentinel,  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )

    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("myslug", file_path, 8100)

    assert (tmp_path / "myslug_workspace").is_dir(), "workspace dir must exist after spawn"
    assert (tmp_path / "myslug.data").is_dir(), "data dir must exist after spawn"
    assert (tmp_path / "myslug_workspace" / "data").is_symlink(), (
        "data symlink must exist after spawn"
    )
    assert (tmp_path / "myslug_workspace" / "myslug.py").is_symlink(), (
        "source symlink must exist after spawn"
    )


def test_spawn_marimo_passes_preexec_fn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """spawn_marimo wires preexec_fn into subprocess.Popen when limits + Linux."""
    from notebook_host import lifecycle

    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda _x: "/usr/bin/uv",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )

    captured: dict[str, object] = {}
    sentinel_proc = _make_fake_popen()

    def fake_popen(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel_proc

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)

    file_path = tmp_path / "x.py"
    file_path.write_text("")
    lifecycle.spawn_marimo(
        "slug",
        file_path,
        8100,
        rlimit_as_bytes=4_000_000_000,
        rlimit_cpu_seconds=3600,
    )
    assert captured.get("preexec_fn") is not None, (
        "spawn_marimo must pass a non-None preexec_fn when rlimits set on Linux"
    )


# ─── sandbox / inline-script-metadata tests ──────────────────────────────────


def test_has_inline_script_metadata_true_when_script_block_present() -> None:
    from notebook_host.lifecycle import has_inline_script_metadata

    source = '# /// script\n# dependencies = ["marimo", "fastf1"]\n# ///\nimport marimo\n'
    assert has_inline_script_metadata(source), (
        "a notebook with a PEP 723 `# /// script` block must be detected as sandbox-eligible"
    )


def test_has_inline_script_metadata_false_for_plain_notebook() -> None:
    from notebook_host.lifecycle import has_inline_script_metadata

    source = "import marimo\n\napp = marimo.App()\n"
    assert not has_inline_script_metadata(source), (
        "a notebook without inline metadata must stay on the baked env (no --sandbox)"
    )


def test_has_inline_script_metadata_false_for_lookalike_comment() -> None:
    from notebook_host.lifecycle import has_inline_script_metadata

    # A comment mentioning the marker mid-line is not a PEP 723 opener.
    source = "import marimo  # /// script is the sandbox opener\n"
    assert not has_inline_script_metadata(source), (
        "only a line that is exactly `# /// script` opens a metadata block"
    )


def _capture_spawn_cmd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, sandbox: bool
) -> list[str]:
    from notebook_host import lifecycle

    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda _x: "/usr/bin/uv",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    captured_args: list[str] = []
    sentinel_proc = _make_fake_popen()

    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> object:
        captured_args.extend(cmd)
        return sentinel_proc

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("myslug", file_path, 8100, sandbox=sandbox)
    return captured_args


def test_spawn_marimo_inserts_sandbox_flag_after_edit_when_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmd = _capture_spawn_cmd(monkeypatch, tmp_path, sandbox=True)
    assert "--sandbox" in cmd, "spawn_marimo(sandbox=True) must pass --sandbox"
    assert cmd[cmd.index("edit") + 1] == "--sandbox", (
        "--sandbox must immediately follow `edit`, before the notebook basename"
    )
    assert cmd.index("--sandbox") < cmd.index("myslug.py"), (
        "--sandbox must precede the file arg so marimo treats it as a flag, not the target"
    )


def test_spawn_marimo_omits_sandbox_flag_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmd = _capture_spawn_cmd(monkeypatch, tmp_path, sandbox=False)
    assert "--sandbox" not in cmd, (
        "a headerless notebook must spawn on the baked env, never --sandbox"
    )


# ─── should_reap (indefinite-TTL sentinel) ────────────────────────────────────


def _make_np(*, alive: bool, age_s: float) -> NotebookProcess:
    """A NotebookProcess whose is_alive()/age_s reflect the given state."""
    import time

    from notebook_host.lifecycle import NotebookProcess

    return NotebookProcess(
        slug="nb",
        port=8100,
        process=_make_fake_popen(alive=alive),
        public_host="localhost",
        host_port=8001,
        started_at=time.time() - age_s,
    )


def test_should_reap_true_for_dead_process_regardless_of_ttl() -> None:
    """A dead subprocess is always reaped — even under an indefinite (0) TTL."""
    from notebook_host.lifecycle import should_reap

    np = _make_np(alive=False, age_s=1.0)
    assert should_reap(np, 7200) is True, "dead process should be reaped under a finite TTL"
    assert should_reap(np, 0) is True, (
        "dead process should be reaped even when age-based reaping is disabled"
    )


def test_should_reap_true_for_alive_process_past_positive_ttl() -> None:
    """An alive subprocess older than a positive TTL is reaped."""
    from notebook_host.lifecycle import should_reap

    np = _make_np(alive=True, age_s=7201.0)
    assert should_reap(np, 7200) is True, "alive process past its TTL should be reaped"


def test_should_reap_false_for_alive_process_within_positive_ttl() -> None:
    """An alive subprocess younger than a positive TTL survives."""
    from notebook_host.lifecycle import should_reap

    np = _make_np(alive=True, age_s=10.0)
    assert should_reap(np, 7200) is False, "alive process within its TTL should not be reaped"


@pytest.mark.parametrize("ttl", [0, -1])
def test_should_reap_false_for_old_alive_process_when_ttl_indefinite(ttl: int) -> None:
    """ttl <= 0 disables age-based reaping: an ancient but alive notebook survives."""
    from notebook_host.lifecycle import should_reap

    np = _make_np(alive=True, age_s=10_000_000.0)
    assert should_reap(np, ttl) is False, (
        f"ttl={ttl} should disable age-based reaping for a live notebook"
    )


# ─── blog (run) mode ──────────────────────────────────────────────────────────


def test_spawn_marimo_uses_run_subcommand_when_mode_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spawn_marimo(mode='run') emits `marimo run`, never `marimo edit`."""
    from notebook_host import lifecycle

    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda _x: "/usr/bin/uv",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    captured: list[str] = []
    sentinel = _make_fake_popen()

    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> object:
        captured.extend(cmd)
        return sentinel

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("myslug", file_path, 8100, mode="run")

    assert "run" in captured, "mode='run' must invoke `marimo run`"
    assert "edit" not in captured, "mode='run' must not invoke `marimo edit`"
    assert "--include-code" not in captured, (
        "run mode must hide source from readers — never pass --include-code"
    )


def test_spawn_marimo_defaults_to_edit_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an explicit mode, spawn_marimo still emits `marimo edit` (back-compat)."""
    from notebook_host import lifecycle

    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda _x: "/usr/bin/uv",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    captured: list[str] = []
    sentinel = _make_fake_popen()

    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> object:
        captured.extend(cmd)
        return sentinel

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("myslug", file_path, 8100)

    assert _marimo_subcommand(captured) == "edit", "default mode must be edit"


def test_spawn_marimo_run_mode_with_sandbox_places_flag_after_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--sandbox must follow `run` and precede the basename (same rule as edit)."""
    from notebook_host import lifecycle

    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda _x: "/usr/bin/uv",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    captured: list[str] = []
    sentinel = _make_fake_popen()

    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> object:
        captured.extend(cmd)
        return sentinel

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    file_path = tmp_path / "myslug.py"
    file_path.write_text("# stub", encoding="utf-8")
    lifecycle.spawn_marimo("myslug", file_path, 8100, mode="run", sandbox=True)

    assert _marimo_subcommand(captured) == "run", "marimo subcommand must be `run`"
    marimo_subcommand_idx = len(captured) - 1 - captured[::-1].index("marimo") + 1
    assert captured[marimo_subcommand_idx + 1] == "--sandbox", (
        "--sandbox must immediately follow `run`"
    )
    assert captured.index("--sandbox") < captured.index("myslug.py"), (
        "--sandbox must precede the notebook basename"
    )


def test_should_reap_false_for_run_mode_even_when_dead() -> None:
    """A blog (run mode) is never killed-and-deleted by should_reap — even if dead.

    Blog liveness/respawn is the sweep's job (Task 6); should_reap must keep its
    hands off so the sweep never source-deletes a blog.
    """
    import time

    from notebook_host.lifecycle import NotebookProcess, should_reap

    dead_blog = NotebookProcess(
        slug="pre-blog",
        port=8100,
        process=_make_fake_popen(alive=False),
        public_host="localhost",
        host_port=8001,
        started_at=time.time() - 10_000_000.0,
        mode="run",
    )
    assert should_reap(dead_blog, 7200) is False, (
        "a run-mode process must never be reaped by should_reap, dead or alive"
    )
    assert should_reap(dead_blog, 0) is False, "indefinite TTL doesn't change the rule"

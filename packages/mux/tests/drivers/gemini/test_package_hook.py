"""The package install hook, run against stub package managers."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from mux.contracts.resources import EnvironmentSpec, NetworkPolicy
from mux.drivers.gemini import compile_environment, inline_bytes, package_sources, packages
from mux.drivers.gemini.packages import HOOKS_TARGET, SCRIPT_TARGET
from mux.errors import UnsupportedCapability

PACKAGES = {"system": ("gh",), "python": ("duckdb",), "npm": ()}
ALLOW = {"decision": "allow"}
INSTALLED = ["apt-get update -qq", "apt-get install -y -qq gh", "python3 -m pip install -q duckdb"]


def stub_managers(tmp_path: Path, *, failing: str = "", slow: float = 0) -> None:
    """Stub apt-get, pip and npm to log their calls; `failing` exits 1 with an error line."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name in ("apt-get", "python3", "npm"):
        fail = f'echo "E: {name} could not fetch" >&2; exit 1' if name == failing else "exit 0"
        (bin_ / name).write_text(f'#!/bin/sh\nsleep {slow}\necho "{name} $*" >>"$CALLS"\n{fail}\n')
    (bin_ / "sudo").write_text('#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
    for stub in bin_.iterdir():
        stub.chmod(0o755)


def run_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, wait: int = 20) -> dict[str, str]:
    """Run the hook script once, as the sandbox does, against the stubbed managers."""
    monkeypatch.setattr(packages, "WAIT_SECONDS", wait)
    script = tmp_path / "hook.sh"
    source = package_sources(PACKAGES)[1]
    assert isinstance(source, dict), "expected an inline script source"
    script.write_text(str(source["content"]))
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "CALLS": str(tmp_path / "calls"),
    }
    done = subprocess.run(
        ["sh", str(script)], input=b"{}", env=env, capture_output=True, check=True, timeout=60
    )
    return json.loads(done.stdout)


def calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls"
    return path.read_text().splitlines() if path.exists() else []


def marker(tmp_path: Path, suffix: str) -> Path:
    found = list(tmp_path.glob(".daimon-packages-*" + suffix))
    return found[0] if found else tmp_path / ("missing" + suffix)


def test_package_sources_when_packages_set_then_hook_runs_before_every_sandbox_tool() -> None:
    """The hook file sits on a documented discovery path and matches every tool call."""
    hooks, script = package_sources(PACKAGES)
    assert isinstance(hooks, dict) and isinstance(script, dict), "expected inline sources"
    assert (hooks["target"], script["target"]) == (HOOKS_TARGET, SCRIPT_TARGET), "hook paths"
    rule = json.loads(str(hooks["content"]))["daimon-packages"]["pre_tool_execution"][0]
    assert rule == {
        "matcher": ".*",
        "hooks": [{"type": "command", "command": f"sh {SCRIPT_TARGET}", "timeout": 120}],
    }, "the hook must run the script before every tool call"
    assert package_sources({"npm": ()}) == [], "no packages, no hook"


def test_hook_timeout_when_compiled_then_well_inside_the_tool_deadline() -> None:
    """A tool call fails near 300 s, hook included, so the gate never waits that long."""
    assert packages.WAIT_SECONDS < packages.TIMEOUT_SECONDS <= 150, "the gate returns early"


def test_hook_when_run_twice_then_installs_once_and_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first tool call installs every manager's packages; the next only allows."""
    stub_managers(tmp_path)
    assert run_hook(tmp_path, monkeypatch) == ALLOW, "a successful install allows the tool call"
    assert run_hook(tmp_path, monkeypatch) == ALLOW, "a later tool call is allowed"
    assert calls(tmp_path) == INSTALLED, "each package installs once, system packages first"


def test_hook_when_install_is_slow_then_denies_within_the_wait_and_installs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install outlives the gated call in a detached worker; a later call sees it done."""
    stub_managers(tmp_path, slow=1.5)
    started = time.monotonic()
    first = run_hook(tmp_path, monkeypatch, wait=1)
    assert time.monotonic() - started < 4, "the hook returns at its wait, not the install's end"
    assert first["decision"] == "deny" and "still installing" in first["reason"], "says why"
    assert run_hook(tmp_path, monkeypatch) == ALLOW, "the background install finishes"
    assert calls(tmp_path) == INSTALLED, "the install ran once, not once per tool call"


def test_hook_when_install_was_killed_then_reclaims_the_lock_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock whose installer died (the runner killed it) must not wedge the environment."""
    stub_managers(tmp_path)
    dead = subprocess.Popen(["true"])
    dead.wait()
    source = package_sources(PACKAGES)[1]
    assert isinstance(source, dict), "expected an inline script source"
    digest = str(source["content"]).split(".daimon-packages-")[1][:16]
    (tmp_path / f".daimon-packages-{digest}.pid").write_text(f"{dead.pid}\n")
    assert run_hook(tmp_path, monkeypatch) == ALLOW, "the retry installs"
    assert calls(tmp_path) == INSTALLED, "the packages install after the dead lock"
    assert marker(tmp_path, ".failed").read_text().count("\n") == 1, "the kill is an attempt"


def test_hook_when_install_keeps_failing_then_retries_and_denies_once_with_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed install retries, then tells the model why once and stops blocking."""
    stub_managers(tmp_path, failing="apt-get")
    first = run_hook(tmp_path, monkeypatch)
    assert first["decision"] == "deny", "the final failure is never silent"
    assert "packages failed" in first["reason"], "the reason names the failure"
    assert "E: apt-get could not fetch" in first["reason"], "the reason quotes the log tail"
    assert run_hook(tmp_path, monkeypatch) == ALLOW, "later calls are not blocked"
    assert calls(tmp_path) == ["apt-get update -qq"] * packages.ATTEMPTS, "retried, then stop"


def test_compile_environment_when_network_set_then_documented_network_shapes() -> None:
    """Limited is an allowlist, none is disabled, and open egress omits the field."""
    limited = NetworkPolicy(mode="limited", allowed_hosts=("api.example.com",))
    shapes = {
        "limited": compile_environment(EnvironmentSpec(name="e", network=limited)),
        "none": compile_environment(EnvironmentSpec(name="e", network=NetworkPolicy(mode="none"))),
        "no hosts": compile_environment(
            EnvironmentSpec(name="e", network=NetworkPolicy(mode="limited"))
        ),
        "open": compile_environment(EnvironmentSpec(name="e", network=NetworkPolicy())),
    }
    assert shapes == {
        "limited": {
            "type": "remote",
            "sources": [],
            "network": {"allowlist": [{"domain": "api.example.com"}]},
        },
        "none": {"type": "remote", "sources": [], "network": "disabled"},
        "no hosts": {"type": "remote", "sources": [], "network": "disabled"},
        "open": {"type": "remote", "sources": []},
    }, "network policies must compile to the documented environment fields"


def test_compile_environment_when_packages_set_then_hook_or_refusal() -> None:
    """Packages add the hook under open egress and are refused where it cannot fetch."""
    compiled = compile_environment(EnvironmentSpec(name="e", packages=PACKAGES))
    assert compiled == {"type": "remote", "sources": package_sources(PACKAGES)}, "hook added"
    assert inline_bytes(compiled) > 0, "the hook spends inline budget"
    for spec in (
        EnvironmentSpec(name="e", packages=PACKAGES, network=NetworkPolicy(mode="none")),
        EnvironmentSpec(name="e", packages={"cargo": ("ripgrep",)}),
    ):
        with pytest.raises(UnsupportedCapability, match="environment_packages"):
            compile_environment(spec)

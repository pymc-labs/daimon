"""The package install hook, run against stub package managers."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from mux.contracts.resources import EnvironmentSpec, NetworkPolicy
from mux.drivers.gemini import compile_environment, inline_bytes, package_sources
from mux.drivers.gemini.packages import HOOKS_TARGET, SCRIPT_TARGET
from mux.errors import UnsupportedCapability

PACKAGES = {"system": ("gh",), "python": ("duckdb",), "npm": ()}
ALLOW = {"decision": "allow"}


def run_hook(tmp_path: Path, *, failing: str = "") -> dict[str, str]:
    """Run the hook script once with apt-get, pip and npm stubbed to log their calls."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name in ("apt-get", "python3", "npm"):
        code = 1 if name == failing else 0
        (bin_ / name).write_text(f'#!/bin/sh\necho "{name} $*" >>"$CALLS"\nexit {code}\n')
    (bin_ / "sudo").write_text('#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
    for stub in bin_.iterdir():
        stub.chmod(0o755)
    script = tmp_path / "hook.sh"
    source = package_sources(PACKAGES)[1]
    assert isinstance(source, dict), "expected an inline script source"
    script.write_text(str(source["content"]))
    env = {
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "CALLS": str(tmp_path / "calls"),
    }
    done = subprocess.run(
        ["sh", str(script)], input=b"{}", env=env, capture_output=True, check=True, timeout=30
    )
    return json.loads(done.stdout)


def test_package_sources_when_packages_set_then_hook_runs_before_every_sandbox_tool() -> None:
    """The hook file sits on a documented discovery path and matches every tool call."""
    hooks, script = package_sources(PACKAGES)
    assert isinstance(hooks, dict) and isinstance(script, dict), "expected inline sources"
    assert (hooks["target"], script["target"]) == (HOOKS_TARGET, SCRIPT_TARGET), "hook paths"
    rule = json.loads(str(hooks["content"]))["daimon-packages"]["pre_tool_execution"][0]
    assert rule == {
        "matcher": ".*",
        "hooks": [{"type": "command", "command": f"sh {SCRIPT_TARGET}", "timeout": 600}],
    }, "the hook must run the script before every tool call"
    assert package_sources({"npm": ()}) == [], "no packages, no hook"


def test_hook_when_run_twice_then_installs_once_and_allows(tmp_path: Path) -> None:
    """The first tool call installs every manager's packages; the next only allows."""
    assert run_hook(tmp_path) == ALLOW, "a successful install allows the tool call"
    assert run_hook(tmp_path) == ALLOW, "a later tool call is allowed"
    assert (tmp_path / "calls").read_text().splitlines() == [
        "apt-get update -qq",
        "apt-get install -y -qq gh",
        "python3 -m pip install -q duckdb",
    ], "each package installs once, system packages first"


def test_hook_when_install_fails_then_denies_once_naming_the_log(tmp_path: Path) -> None:
    """A failed install tells the model once, then stops blocking tool calls."""
    first = run_hook(tmp_path, failing="apt-get")
    assert first["decision"] == "deny" and "packages failed" in first["reason"], "deny once"
    assert run_hook(tmp_path, failing="apt-get") == ALLOW, "later calls are not blocked"
    assert (tmp_path / "calls").read_text().splitlines() == ["apt-get update -qq"], "no retry"


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

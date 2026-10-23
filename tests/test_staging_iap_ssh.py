"""Exercise the real IAP SSH registration/status scripts with fake gcloud."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts/ci/iap-ssh-key.sh"
_WORKFLOW = _ROOT / ".github/workflows/deploy.yml"

#: deploy.yml appends this to every step after its freshness check
#: (tests/test_deploy_superseded.py proves it); a step's own condition is the rest.
_FRESHNESS = " && steps.fresh.outputs.superseded != 'true'"


def without_freshness(condition: str) -> str:
    own = condition.removesuffix(_FRESHNESS)
    return own[1:-1] if own != condition and own.startswith("(") and own.endswith(")") else own


def _fake_tools(tmp_path: Path, *, mode: str = "success") -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_gcloud = bindir / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env python3
import os
import pathlib
import sys
import time

args = sys.argv[1:]
log = pathlib.Path(os.environ["FAKE_GCLOUD_LOG"])
with log.open("a") as out:
    out.write(" ".join(args) + "\\n")

if args[:2] == ["compute", "ssh"]:
    vm = args[2]
    if vm.endswith("-notebook"):
        entered = os.environ.get("FAKE_NOTEBOOK_ENTERED")
        released = os.environ.get("FAKE_NOTEBOOK_RELEASED")
        if entered and released:
            pathlib.Path(entered).touch()
            deadline = time.monotonic() + 5
            while not pathlib.Path(released).exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not pathlib.Path(released).exists():
                print("blocked notebook probe timed out", file=sys.stderr)
                sys.exit(8)
        if os.environ.get("FAKE_MODE") == "notebook-failure":
            sys.exit(7)
        sys.exit(0)
    if vm.endswith("-workers"):
        if os.environ.get("FAKE_MODE") == "worker-failure":
            sys.exit(1)
        if os.environ.get("FAKE_MODE") == "retry-worker":
            count_file = pathlib.Path(os.environ["FAKE_WORKER_COUNT"])
            count = int(count_file.read_text()) if count_file.exists() else 0
            count_file.write_text(str(count + 1))
            if count < 2:
                sys.exit(1)
        sys.exit(0)
if args[:3] == ["compute", "instances", "describe"]:
    print(os.environ.get("FAKE_WORKERS_STATUS", "RUNNING"))
    sys.exit(0)
print("unexpected gcloud invocation: " + " ".join(args), file=sys.stderr)
sys.exit(90)
"""
    )
    fake_gcloud.chmod(0o755)
    fake_sleep = bindir / "sleep"
    fake_sleep.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "seconds = sys.argv[1]\n"
        "with open(os.environ['FAKE_SLEEP_LOG'], 'a') as out: out.write(f'sleep {seconds}\\n')\n"
        "if seconds == '1': time.sleep(0.05)\n"
    )
    fake_sleep.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_GCLOUD_LOG": str(tmp_path / "gcloud.log"),
        "FAKE_SLEEP_LOG": str(tmp_path / "sleep.log"),
        "FAKE_MODE": mode,
        "FAKE_WORKER_COUNT": str(tmp_path / "worker-count"),
    }


def _run_registration(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_SCRIPT, "register", "test-repo", "test-zone"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_staging_key_registration_join_waits_and_propagates_worker_retries(
    tmp_path: Path,
) -> None:
    env = _fake_tools(tmp_path, mode="retry-worker")
    entered = tmp_path / "notebook-entered"
    released = tmp_path / "notebook-released"
    env["FAKE_NOTEBOOK_ENTERED"] = str(entered)
    env["FAKE_NOTEBOOK_RELEASED"] = str(released)
    state_dir = tmp_path / "state"

    registration = subprocess.Popen(
        [_SCRIPT, "run", state_dir, "30", "test-repo", "test-zone"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 3
    while not entered.exists() and registration.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entered.exists(), "the registration operation should reach its notebook probe"

    join = subprocess.Popen(
        [_SCRIPT, "wait", state_dir, "10"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.05)
    assert join.poll() is None, "join must not pass while the notebook key probe is blocked"
    assert not (state_dir / "status").exists(), "status appears only after registration completes"
    assert not (tmp_path / "worker-count").exists(), (
        "workers are not probed before notebook key creation"
    )

    released.touch()
    assert registration.wait(timeout=3) == 0
    stdout, stderr = join.communicate(timeout=3)
    assert join.returncode == 0, f"join failed: {stdout}\n{stderr}"
    assert (state_dir / "status").read_text() == "0\n"
    calls = Path(env["FAKE_GCLOUD_LOG"]).read_text().splitlines()
    assert sum("compute ssh test-repo-workers" in call for call in calls) == 3
    sleeps = Path(env["FAKE_SLEEP_LOG"]).read_text().splitlines()
    assert sleeps.count("sleep 10") == 2


def test_join_fails_on_registration_failure_instead_of_releasing_refresh_gate(
    tmp_path: Path,
) -> None:
    env = _fake_tools(tmp_path, mode="worker-failure")
    state_dir = tmp_path / "state"
    registration = subprocess.Popen(
        [_SCRIPT, "run", state_dir, "30", "test-repo", "test-zone"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert registration.wait(timeout=3) == 0

    join = subprocess.run(
        [_SCRIPT, "wait", state_dir, "2"], env=env, capture_output=True, text=True, check=False
    )
    assert join.returncode == 1
    assert "IAP SSH registration failed" in join.stderr
    assert "workers VM did not accept the SSH key" in join.stdout
    calls = Path(env["FAKE_GCLOUD_LOG"]).read_text().splitlines()
    assert sum("compute ssh test-repo-workers" in call for call in calls) == 6


def test_join_rejects_incomplete_and_missing_status(tmp_path: Path) -> None:
    env = _fake_tools(tmp_path)
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "status").write_text("not-a-final-status\n")
    result = subprocess.run(
        [_SCRIPT, "wait", incomplete, "2"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "status is incomplete or invalid" in result.stderr

    missing = subprocess.run(
        [_SCRIPT, "wait", tmp_path / "missing", "1"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 1
    assert "status missing after 1s" in missing.stderr


def test_staging_prebuilt_join_precedes_both_vm_refreshes() -> None:
    workflow: dict[str, Any] = yaml.safe_load(_WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    start = steps[names.index("Start staging IAP SSH key registration")]
    join = steps[names.index("Wait for staging IAP SSH key registration")]
    serial = steps[names.index("Register the IAP SSH key")]
    assert (
        without_freshness(start["if"])
        == "inputs.environment == 'staging' && steps.images.outputs.prebuilt == 'true'"
    )
    assert join["if"] == start["if"]
    assert (
        without_freshness(serial["if"])
        == "inputs.environment != 'staging' || steps.images.outputs.prebuilt != 'true'"
    )
    assert names.index("Wait for staging IAP SSH key registration") < names.index(
        "Start notebook-host VM refresh (in the background)"
    )
    assert names.index("Wait for staging IAP SSH key registration") < names.index(
        "Refresh worker VM containers (recreate in place)"
    )
    assert "iap-ssh-key.sh run " in start["run"]
    assert "iap-ssh-key.sh wait " in join["run"]


def test_serial_registration_step_works_without_checked_out_helper(tmp_path: Path) -> None:
    workflow: dict[str, Any] = yaml.safe_load(_WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    serial = next(step for step in steps if step.get("name") == "Register the IAP SSH key")
    run = serial["run"].replace("${{ vars.GCP_AR_REPO }}", "test-repo")
    env = _fake_tools(tmp_path, mode="retry-worker")
    env["ZONE"] = "test-zone"
    assert not (tmp_path / "scripts/ci/iap-ssh-key.sh").exists()

    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", run],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = Path(env["FAKE_GCLOUD_LOG"]).read_text().splitlines()
    assert calls[0].startswith("compute ssh test-repo-notebook")
    assert calls[1].startswith("compute instances describe test-repo-workers")
    assert sum("compute ssh test-repo-workers" in call for call in calls) == 3

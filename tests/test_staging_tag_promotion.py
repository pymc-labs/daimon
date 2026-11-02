"""Exercise the staging promotion step against a stateful fake Artifact Registry."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DIGESTS = {
    "notebook-host": "sha256:" + "a" * 64,
    "report-host": "sha256:" + "b" * 64,
    "daimon": "sha256:" + "c" * 64,
}
_TAG = "a747f4a"
_FAKE_GCLOUD = r"""#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_REGISTRY_STATE"])
log_path = Path(os.environ["FAKE_GCLOUD_LOG"])
args = sys.argv[1:]
with log_path.open("a") as log:
    log.write(json.dumps(args) + "\n")
state = json.loads(state_path.read_text())

def flag(name):
    return next(arg.split("=", 1)[1] for arg in args if arg.startswith(name + "="))

if args[:3] == ["artifacts", "docker", "tags"]:
    print("PERMISSION_DENIED: Permission 'artifactregistry.tags.delete' denied", file=sys.stderr)
    sys.exit(1)
if args[:3] == ["artifacts", "tags", "list"]:
    if os.environ.get("FAKE_LOOKUP_FAIL") == "1":
        print("UNAVAILABLE: registry lookup failed", file=sys.stderr)
        sys.exit(1)
    assert flag("--format") == "value(version)"
    match = re.fullmatch(r'name="projects/test-project/locations/us-central1/repositories/test-repo/packages/([^/]+)/tags/([^/]+)"', flag("--filter"))
    assert match is not None
    value = state.get(match.group(1) + ":" + match.group(2))
    if value:
        print(value)
    sys.exit(0)
if args[:3] == ["artifacts", "tags", "create"]:
    key = flag("--package") + ":" + args[3]
    version = flag("--version")
    race = os.environ.get("FAKE_CREATE_RACE")
    if race and key not in state:
        state[key] = version if race == "same" else "sha256:" + "d" * 64
        state_path.write_text(json.dumps(state))
    if key in state:
        print("ALREADY_EXISTS: tag exists", file=sys.stderr)
        sys.exit(1)
    state[key] = version
    state_path.write_text(json.dumps(state))
    sys.exit(0)
raise AssertionError("Unexpected gcloud command: " + repr(args))
"""


@pytest.fixture
def registry(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text(_FAKE_GCLOUD)
    fake.chmod(0o755)
    state = tmp_path / "state.json"
    log = tmp_path / "commands.jsonl"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_REGISTRY_STATE": str(state),
        "FAKE_GCLOUD_LOG": str(log),
        "PROJECT_ID": "test-project",
        "REGION": "us-central1",
        "REPO": "test-repo",
        "AR_HOST": "us-central1-docker.pkg.dev",
    }
    return state, log, env


def _script() -> str:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/deploy.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    script = next(
        step["run"] for step in steps if step.get("name") == "Mark tested staging images promotable"
    )
    for package, output in (
        ("notebook-host", "notebook"),
        ("report-host", "report"),
        ("daimon", "daimon"),
    ):
        source = f"us-central1-docker.pkg.dev/test-project/test-repo/{package}@{_DIGESTS[package]}"
        target = f"us-central1-docker.pkg.dev/test-project/test-repo/{package}:{_TAG}"
        script = script.replace(f"${{{{ steps.record.outputs.{output} }}}}", source)
        script = script.replace(f"${{{{ steps.tags.outputs.{output} }}}}", target)
    assert "${{" not in script
    return script


def _run(
    state: Path, env: dict[str, str], tags: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    state.write_text(json.dumps(tags))
    return subprocess.run(
        ["bash", "-e", "-c", _script()], env=env, capture_output=True, text=True, check=False
    )


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_existing_same_digest_reuses_tag_without_delete_permission(registry) -> None:
    state, log, env = registry
    tags = {f"{package}:{_TAG}": digest for package, digest in _DIGESTS.items()}
    state.write_text(json.dumps(tags))
    legacy = subprocess.run(
        ["gcloud", "artifacts", "docker", "tags", "add", "source", "tag", "--quiet"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert legacy.returncode == 1
    assert "artifactregistry.tags.delete" in legacy.stderr

    result = _run(state, env, tags)
    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text()) == tags
    assert not any(call[:3] == ["artifacts", "tags", "create"] for call in _calls(log))


def test_missing_tags_are_created_in_gate_order(registry) -> None:
    state, log, env = registry
    result = _run(state, env, {})
    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text()) == {
        f"{package}:{_TAG}": digest for package, digest in _DIGESTS.items()
    }
    creates = [call for call in _calls(log) if call[:3] == ["artifacts", "tags", "create"]]
    assert [next(arg for arg in call if arg.startswith("--package=")) for call in creates] == [
        "--package=notebook-host",
        "--package=report-host",
        "--package=daimon",
    ]


def test_different_existing_digest_fails_without_overwriting(registry) -> None:
    state, log, env = registry
    tags = {f"notebook-host:{_TAG}": "sha256:" + "d" * 64}
    result = _run(state, env, tags)
    assert result.returncode != 0
    assert "points to" in result.stderr
    assert json.loads(state.read_text()) == tags
    assert not any(call[:3] == ["artifacts", "tags", "create"] for call in _calls(log))


def test_lookup_failure_is_not_treated_as_missing(registry) -> None:
    state, log, env = registry
    env["FAKE_LOOKUP_FAIL"] = "1"
    result = _run(state, env, {})
    assert result.returncode != 0
    assert "Could not inspect canonical tag" in result.stderr
    assert json.loads(state.read_text()) == {}
    assert not any(call[:3] == ["artifacts", "tags", "create"] for call in _calls(log))


@pytest.mark.parametrize("race,success", [("same", True), ("different", False)])
def test_concurrent_creation_preserves_existing_tag(registry, race: str, success: bool) -> None:
    state, _, env = registry
    env["FAKE_CREATE_RACE"] = race
    result = _run(state, env, {})
    assert (result.returncode == 0) is success
    tags = json.loads(state.read_text())
    if success:
        assert tags == {f"{package}:{_TAG}": digest for package, digest in _DIGESTS.items()}
    else:
        assert tags == {f"notebook-host:{_TAG}": "sha256:" + "d" * 64}
        assert "Could not safely publish" in result.stderr

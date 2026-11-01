"""Keep the CI verdict and image-promotion gates linked as workflows evolve."""

import glob
import importlib.util
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_ROOT = _WORKFLOWS.parents[1]

#: deploy.yml appends this to every step after its freshness check
#: (tests/test_deploy_superseded.py proves it); a step's own condition is the rest.
_FRESHNESS = " && steps.fresh.outputs.superseded != 'true'"


def without_freshness(condition: str) -> str:
    own = condition.removesuffix(_FRESHNESS)
    return own[1:-1] if own != condition and own.startswith("(") and own.endswith(")") else own


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOWS / name).read_text())


_spec = importlib.util.spec_from_file_location("ci_plan", _ROOT / "scripts/ci/plan.py")
assert _spec and _spec.loader
_plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plan)


def _legs(job: dict[str, Any]) -> list[dict[str, Any]]:
    return _plan.matrix_legs(job)


def _pytest_commands(job: dict[str, Any]) -> list[str]:
    """Every `uv run pytest` command of a job, once per matrix leg with the leg's values filled in."""
    legs = _legs(job) or [{}]
    commands: list[str] = []
    for leg in legs:
        for step in job.get("steps", []):
            command = " ".join(step.get("run", "").split())
            if "uv run pytest" not in command:
                continue
            for key, value in leg.items():
                command = command.replace(f"${{{{ matrix.{key} }}}}", str(value))
            commands.append(command)
    return commands


def test_ci_pytest_commands_collect_top_level_and_performance_tests() -> None:
    """Expand the paths passed to pytest, so newly added tests remain selected."""
    selected: set[Path] = set()
    for job in _workflow("ci.yml")["jobs"].values():
        for command in _pytest_commands(job):
            tokens = shlex.split(command)
            paths = tokens[tokens.index("pytest") + 1 :]
            for token in paths:
                if token.startswith("-"):
                    break
                for match in glob.glob(str(_ROOT / token)):
                    path = Path(match)
                    if path.is_dir():
                        selected.update(path.rglob("test_*.py"))
                    elif path.is_file():
                        selected.add(path)

    expected = {
        *(_ROOT / "tests").glob("test_*.py"),
        *(_ROOT / "tests/performance").glob("test_*.py"),
    }
    assert expected, "the selection check needs test files to inspect"
    assert expected <= selected, f"CI omits: {sorted(expected - selected)}"


def test_shards_and_candidate_are_all_deploy_prerequisites() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    deploy = jobs["deploy-gcp"]
    legs = _legs(jobs["pytest-db"])
    discord_legs = _legs(jobs["pytest-discord"])
    for suite, count in (("core", 6), ("mcp", 2), ("discord", 2)):
        suite_legs = [leg for leg in legs + discord_legs if leg["suite"] == suite]
        shards = [leg["shard"] for leg in suite_legs]
        assert shards == list(range(count)), f"{suite} must run every shard exactly once"
        assert {leg["shards"] for leg in suite_legs} == {count}
    # The sharded legs of one suite select the same paths, or a shard would skip tests.
    for suite in {leg["suite"] for leg in legs + discord_legs}:
        assert len({leg["paths"] for leg in legs + discord_legs if leg["suite"] == suite}) == 1
    assert {leg["suite"] for leg in legs} == {"core", "mcp", "slack", "teams", "web", "parity"}
    assert {leg["suite"] for leg in discord_legs} == {"discord"}
    for name in ("pytest-db", "pytest-discord"):
        shard_options = [
            " ".join(step["run"].split())
            for step in jobs[name]["steps"]
            if "uv run pytest" in step.get("run", "")
        ]
        assert len(shard_options) == 1
        assert "-p daimon.testing.ci_shard" in shard_options[0]
        assert "--ci-shard-index=${{ matrix.shard }}" in shard_options[0]
        assert "--ci-shard-count=${{ matrix.shards }}" in shard_options[0]
        assert "--ci-shard-by=file" in shard_options[0]
        assert "--ci-shard-suite=${{ matrix.suite }}" in shard_options[0]
    assert set(deploy["needs"]) == {
        "lint",
        "shard-check",
        # The production default path (mux) gates the deploy.
        "pytest-mux-leg",
        "pytest-db",
        "pytest-discord",
        "pytest-light",
        "mux-standalone",
        "pytest-notebook-host",
        "pytest-staging-config",
        # A red offline journey (J1-J10) blocks the staging deploy.
        "journeys-offline",
        "docker-publish-candidate",
    }
    assert {leg["suite"] for leg in _legs(jobs["pytest-light"])} == {
        "report-host",
        "mux",
        "harness",
        "harness-conformance",
    }
    # No status override: GitHub's default needs gate skips after failure/skip.
    assert not any(status in deploy["if"] for status in ("always()", "failure()", "cancelled()"))
    assert "github.event_name == 'push'" in jobs["docker-publish-candidate"]["if"]
    assert "refs/heads/main" in jobs["docker-publish-candidate"]["if"]
    assert "vars.GCP_WIF_PROVIDER == ''" in jobs["docker-build"]["if"]
    assert "docker-publish-candidate" not in jobs["docker-build"].get("needs", [])


def test_pytest_jobs_use_four_workers_with_worksteal_except_discord() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    pytest_jobs = {name: job for name, job in jobs.items() if _pytest_commands(job)}
    assert set(pytest_jobs) == {
        "pytest-db",
        "pytest-discord",
        "pytest-mux-leg",
        "pytest-staging-config",
        "pytest-light",
        "pytest-notebook-host",
        "journeys-offline",
    }
    for name, job in pytest_jobs.items():
        for command in _pytest_commands(job):
            assert "-n auto" not in command
            if name in (
                "pytest-discord",
                "pytest-mux-leg",
                "pytest-staging-config",
                "journeys-offline",
            ):
                assert " -n 2 " in command
            elif "-m slow" not in command:
                assert " -n 4 --dist worksteal " in command
            if "-m slow" not in command:
                assert "--durations=20" in command
                assert "--junitxml=" in command


def test_core_legs_check_out_full_history() -> None:
    """Core's credential-equivalence and native-bounds tests need old commits and branches.

    In a GitHub expression `0` is falsy, so `cond && 0 || 1` quietly yields 1; the depth
    therefore comes from an explicit per-leg key.
    """
    job = _workflow("ci.yml")["jobs"]["pytest-db"]
    checkout = next(step for step in job["steps"] if "actions/checkout" in step.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == "${{ matrix.fetch-depth }}"
    legs = _legs(job)
    core = [leg for leg in legs if leg["suite"] == "core"]
    assert len(core) == 6
    assert all(leg["fetch-depth"] == 0 for leg in core), "core must fetch every branch and commit"
    assert all(isinstance(leg["fetch-depth"], int) for leg in legs)


@pytest.mark.parametrize("name", ["ci.yml", "contract.yml", "docs.yml", "formal-models.yml"])
def test_no_fetch_depth_uses_a_falsy_zero_in_an_expression(name: str) -> None:
    for line in (_WORKFLOWS / name).read_text().splitlines():
        if "fetch-depth" in line and "${{" in line:
            assert not re.search(r"&&\s*0\s*\|\|", line), line


def test_shard_check_runs_the_checker_against_the_matrix() -> None:
    steps = _workflow("ci.yml")["jobs"]["shard-check"]["steps"]
    assert any(step.get("run") == "uv run python scripts/check_ci_shards.py" for step in steps)


def test_every_sharded_job_loads_the_plugin_the_checker_discovers() -> None:
    """check_ci_shards.py proves the jobs whose pytest loads the plugin; a matrix
    with `shards` but no plugin would run every test on every leg, unchecked."""
    jobs = _workflow("ci.yml")["jobs"]
    with_plugin = {
        name
        for name, job in jobs.items()
        if any("-p daimon.testing.ci_shard" in command for command in _pytest_commands(job))
    }
    with_shards = {name for name, job in jobs.items() if any("shards" in leg for leg in _legs(job))}
    assert (
        with_plugin
        == with_shards
        == {
            "pytest-db",
            "pytest-discord",
            "pytest-mux-leg",
        }
    )


def test_every_discord_leg_keeps_the_hang_diagnostics() -> None:
    job = _workflow("ci.yml")["jobs"]["pytest-discord"]
    assert job["strategy"]["fail-fast"] is False
    pytest_step = next(step for step in job["steps"] if step.get("id") == "discord-pytest")
    assert pytest_step["env"]["DAIMON_PYTEST_HANG_DIR"] == "${{ runner.temp }}"
    assert "-o faulthandler_timeout=300" in pytest_step["run"]
    assert "discord-postgres-diagnostics.sh" in pytest_step["run"]
    diagnostics = next(
        step for step in job["steps"] if step.get("name") == "Discord hang diagnostics"
    )
    assert diagnostics["if"] == "always() && steps.discord-pytest.outcome != 'success'"


@pytest.mark.parametrize("name", ["ci.yml", "contract.yml", "docs.yml", "formal-models.yml"])
def test_third_party_actions_are_pinned_to_commit_shas(name: str) -> None:
    text = (_WORKFLOWS / name).read_text()
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, flags=re.MULTILINE)
    assert uses
    for action in uses:
        if action.startswith("./"):
            continue
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), action


def test_main_pushes_and_deploys_are_never_cancelled() -> None:
    concurrency = _workflow("ci.yml")["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    # Only a pull request shares a group (its ref) with the run it supersedes.
    assert concurrency["group"] == (
        "ci-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}"
    )


def test_candidate_is_not_promotable_until_staging_gates_pass() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    candidate = jobs["docker-publish-candidate"]
    steps = candidate["steps"]
    tag_script = next(step["run"] for step in steps if step.get("name") == "Compute candidate tags")
    assert 'TAG="ci-${{ github.run_id }}-${{ github.run_attempt }}"' in tag_script
    assert "$SHORT" not in tag_script
    for image in ("daimon", "notebook", "report"):
        assert candidate["outputs"][f"{image}-digest"].endswith(".outputs.digest }}")
        assert jobs["deploy-gcp"]["with"][f"{image}_digest"] == (
            f"${{{{ needs.docker-publish-candidate.outputs.{image}-digest }}}}"
        )

    deploy = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in deploy]
    promotable = names.index("Mark tested staging images promotable")
    for gate in (
        "Run migrations (blocking gate)",
        "Deploy MCP registered minters",
        "Drain legacy MCP turns before first verifier cutover",
        "Refresh worker VM containers (recreate in place)",
        "Wait for notebook-host VM refresh",
        "Health gate",
        "Post-deploy smoke turn",
        "Let the adapters' boot reconcile settle",
        "Verify deployed installs match shipped defaults",
    ):
        assert names.index(gate) < promotable
    script = deploy[promotable]["run"]
    assert "gcloud artifacts tags create" in script
    assert "gcloud artifacts docker tags add" not in script
    assert script.rfind("steps.tags.outputs.daimon") > script.rfind("steps.tags.outputs.report")
    assert without_freshness(deploy[promotable]["if"]) == "steps.images.outputs.prebuilt == 'true'"


def test_defaults_verify_qa_exception_is_staging_only() -> None:
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    verify = next(
        step
        for step in steps
        if step.get("name") == "Verify deployed installs match shipped defaults"
    )
    assert verify["env"]["STAGING_QA_TENANT"] == "${{ vars.STAGING_QA_TENANT }}"
    script = verify["run"]
    assert (
        'if [ "${{ inputs.environment }}" = staging ] && [ -n "$STAGING_QA_TENANT" ]; then'
        in script
    )
    assert "--warn-tenant $STAGING_QA_TENANT" in script
    assert "defaults verify $verify_tenant_arg" in script


def test_prebuilt_staging_is_pinned_and_production_rebuilds() -> None:
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    select = next(step for step in steps if step.get("id") == "images")
    assert "Prebuilt digests are restricted to staging" in select["run"]
    assert "^sha256:[0-9a-f]{64}$" in select["run"]
    assert "gcloud artifacts docker images describe" in select["run"]
    assert "Prebuilt digests need the selective main CI path" in select["run"]
    builds = {
        step["name"]: without_freshness(step["if"])
        for step in steps
        if step.get("name", "").startswith("Build + push")
    }
    # A build lands on the tag the images step chose, never straight on the short SHA.
    assert {
        step["with"]["tags"] for step in steps if step.get("name", "").startswith("Build + push")
    } == {f"${{{{ steps.images.outputs.{name} }}}}" for name in ("daimon", "notebook", "report")}
    assert builds == {
        f"Build + push {name} image": f"steps.images.outputs.build_{output} == 'true'"
        for name, output in (
            ("daimon", "daimon"),
            ("notebook-host", "notebook"),
            ("report-host", "report"),
        )
    }
    promote = _workflow("promote.yml")["jobs"]
    assert promote["deploy"]["needs"] == "validate"
    assert promote["deploy"]["with"]["environment"] == "production"
    assert "daimon_digest" not in promote["deploy"]["with"]


_DIGEST = {
    name: "sha256:" + char * 64
    for name, char in (("daimon", "a"), ("notebook", "b"), ("report", "c"))
}
_RECORDED = {
    "daimon": "example.test/test-project/images/daimon@sha256:" + "d" * 64,
    "notebook": "example.test/test-project/images/notebook-host@sha256:" + "e" * 64,
    "report": "example.test/test-project/images/report-host@sha256:" + "f" * 64,
}
_TAGS = {name: f"example/{name}:abc1234" for name in ("daimon", "notebook", "report")}
_RUN_TAG = "deploy-7-1"


@pytest.mark.parametrize(
    ("environment", "selective", "digests", "changed", "expected"),
    [
        # Manual and production deploys build every image from source.
        ("staging", False, {}, (), {"prebuilt": "false", **_TAGS}),
        ("production", False, {}, (), {"prebuilt": "false", **_TAGS}),
        ("staging", False, _DIGEST, (), None),
        ("production", True, _DIGEST, (), None),
        # Main CI: this run's candidates, else the deployed image, else a build.
        ("staging", True, _DIGEST, ("daimon", "notebook", "report"), {
            "prebuilt": "true",
            **{name: f"example.test/test-project/images/{pkg}@{_DIGEST[name]}"
               for name, pkg in (("daimon", "daimon"), ("notebook", "notebook-host"), ("report", "report-host"))},
        }),
        ("staging", True, {"daimon": _DIGEST["daimon"]}, ("daimon",), {
            "daimon": f"example.test/test-project/images/daimon@{_DIGEST['daimon']}",
            "notebook": _RECORDED["notebook"],
            "report": _RECORDED["report"],
            "build_notebook": "false",
        }),
        ("staging", True, {}, ("report",), {
            "daimon": _RECORDED["daimon"],
            "report": f"example.test/test-project/images/report-host:{_RUN_TAG}",
            "build_daimon": "false",
            "build_report": "true",
        }),
        ("staging", True, {"daimon": "sha256:" + "A" * 64}, (), None),
        ("staging", True, {"daimon": _DIGEST["daimon"] + "\nextra"}, (), None),
    ],
)  # fmt: skip
def test_image_selection_shell_validation(
    tmp_path: Path,
    environment: str,
    selective: bool,
    digests: dict[str, str],
    changed: tuple[str, ...],
    expected: dict[str, str] | None,
) -> None:
    """Execute the workflow's Bash, including its full-value digest check."""
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "images")
    for expression, value in {
        "${{ inputs.environment }}": environment,
        "${{ vars.GCP_PROJECT_ID }}": "test-project",
        **{f"${{{{ steps.tags.outputs.{name} }}}}": tag for name, tag in _TAGS.items()},
    }.items():
        script = script.replace(expression, value)
    # Reads the image a VM boots, and accepts any manifest it is asked about.
    fake_gcloud = "\n".join(
        [
            "gcloud() {",
            '  case "$*" in',
            *(f'    *"extract({key}-image)"*) echo "$REC_{key.upper()}" ;;' for key in ("daimon", "notebook", "report")),
            "  esac",
            "}",
            "",
        ]
    )  # fmt: skip
    output = tmp_path / "outputs"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "AR_HOST": "example.test",
        "REPO": "images",
        "ZONE": "zone-a",
        "VM_PREFIX": "env-daimon",
        "SELECTIVE": str(selective).lower(),
        "RUN_TAG": _RUN_TAG,
        **{f"{name.upper()}_DIGEST": digests.get(name, "") for name in _TAGS},
        **{f"{name.upper()}_CHANGED": str(name in changed).lower() for name in _TAGS},
        **{f"REC_{name.upper()}": ref for name, ref in _RECORDED.items()},
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", fake_gcloud + script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if expected is None:
        assert result.returncode != 0
        assert not output.exists() or "prebuilt=true" not in output.read_text().splitlines()
        return
    assert result.returncode == 0, result.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs == outputs | expected


@pytest.mark.parametrize(
    ("recorded", "served"),
    [
        # From another registry path: never reused.
        ("elsewhere/daimon@sha256:" + "d" * 64, True),
        # Deleted from the registry since it was deployed.
        (_RECORDED["daimon"], False),
    ],
)
def test_unusable_deployed_image_is_rebuilt(tmp_path: Path, recorded: str, served: bool) -> None:
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "images")
    script = script.replace("${{ inputs.environment }}", "staging")
    script = script.replace("${{ vars.GCP_PROJECT_ID }}", "test-project")
    for name, tag in _TAGS.items():
        script = script.replace(f"${{{{ steps.tags.outputs.{name} }}}}", tag)
    output = tmp_path / "outputs"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "AR_HOST": "example.test",
        "REPO": "images",
        "ZONE": "zone-a",
        "VM_PREFIX": "env-daimon",
        "SELECTIVE": "true",
        "RUN_TAG": _RUN_TAG,
        **{f"{name.upper()}_CHANGED": "false" for name in _TAGS},
    }
    fake = (
        f'gcloud() {{ case "$*" in *extract*) echo "{recorded}" ;; '
        f"*images\\ describe*) return {0 if served else 1} ;; esac; }}\n"
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", fake + script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["build_daimon"] == "true"
    assert outputs["daimon"] == f"example.test/test-project/images/daimon:{_RUN_TAG}"


_RECORD_STEP = "Record deployed image digests for VM boot"
_FAKE_GCLOUD = r"""
gcloud() {
  printf '%s\n' "$*" >> "$FAKE_LOG"
  case "$*" in
    "artifacts docker images describe "*)
      case "$5" in
        *@sha256:*) printf '%s\n' "${5#*@}" ;;
        *) printf 'sha256:%s\n' "$(printf '%s' "$5" | sha256sum | cut -c1-64)" ;;
      esac ;;
    "compute instances add-metadata "*)
      [ -z "${FAKE_FAIL_WRITE:-}" ] || return 1
      printf '%s\n' "$4 ${8%%=*} ${8#*=}" >> "$FAKE_STATE" ;;
    "compute instances describe "*)
      key="${7#--format=value(metadata.items.extract(}"
      grep -F "$4 ${key%).flatten())} " "$FAKE_STATE" | tail -n 1 | cut -d' ' -f3 ;;
    *) return 1 ;;
  esac
}
"""


def _record_step() -> dict[str, Any]:
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    return next(step for step in steps if step.get("name") == _RECORD_STEP)


def test_boot_digests_are_recorded_after_every_gate_and_before_promotion() -> None:
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    names = [step.get("name", "") for step in steps]
    record = names.index(_RECORD_STEP)
    assert names.index("Health gate") < record
    assert names.index("Wait for notebook-host VM refresh") < record
    assert names.index("Verify deployed installs match shipped defaults") < record
    assert record < names.index("Mark tested staging images promotable")
    assert _record_step()["if"] == "steps.fresh.outputs.superseded != 'true'"


def test_boot_digests_write_only_the_deploy_owned_keys() -> None:
    script = _record_step()["run"]
    assert "daimon-secret-env" not in script
    assert "startup-script" not in script
    keys = re.findall(r"record \"\$VM_PREFIX-\w+\" ([\w-]+)", script)
    assert keys == ["daimon-image", "notebook-image", "report-image", "deployed-sha"]


@pytest.mark.parametrize("fail_write", [False, True])
def test_boot_digests_script_records_full_digest_references(
    tmp_path: Path, fail_write: bool
) -> None:
    step = _record_step()
    base = "example.test/proj/images"
    sha = "0123456789abcdef" * 2 + "01234567"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "FAKE_LOG": str(tmp_path / "log"),
        "FAKE_STATE": str(tmp_path / "state"),
        "ZONE": "zone-a",
        "VM_PREFIX": "env-daimon",
        "DAIMON_IMAGE": f"{base}/daimon:abc1234",
        "NOTEBOOK_IMAGE": f"{base}/notebook-host@sha256:{'b' * 64}",
        "REPORT_IMAGE": f"{base}/report-host:abc1234",
    }
    if fail_write:
        env["FAKE_FAIL_WRITE"] = "1"
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-o",
            "pipefail",
            "-c",
            "sleep() { :; }\n" + _FAKE_GCLOUD + step["run"].replace("${{ inputs.sha }}", sha),
        ],  # fmt: skip
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if fail_write:
        assert result.returncode != 0
        assert "Could not record daimon-image on env-daimon-workers" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    written: dict[tuple[str, str], str] = {}
    for line in (tmp_path / "state").read_text().splitlines():
        vm, key, value = line.split(" ")
        written[(vm, key)] = value
    assert written.pop(("env-daimon-workers", "deployed-sha")) == sha
    assert set(written) == {
        ("env-daimon-workers", "daimon-image"),
        ("env-daimon-notebook", "notebook-image"),
        ("env-daimon-notebook", "report-image"),
    }
    for value in written.values():
        assert re.fullmatch(rf"{re.escape(base)}/[a-z-]+@sha256:[0-9a-f]{{64}}", value)
    assert written[("env-daimon-notebook", "notebook-image")].endswith("b" * 64)
    assert "daimon:" not in "".join(written.values())
    # The promotion step tags these pinned references, never the build tags.
    outputs = dict(line.split("=", 1) for line in (tmp_path / "outputs").read_text().splitlines())
    assert outputs == {
        "daimon": written[("env-daimon-workers", "daimon-image")],
        "notebook": written[("env-daimon-notebook", "notebook-image")],
        "report": written[("env-daimon-notebook", "report-image")],
    }


@pytest.mark.parametrize(
    "state",
    [
        "first",
        "repeat",
        "missing-minters",
        "missing-service",
        "resume-live",
        "resume-expired",
        "missing-cutoff",
    ],
)
def test_mcp_cutover_shell_drains_every_minter_before_strict_admission(
    tmp_path: Path, state: str
) -> None:
    """Execute both actual deploy phases offline, including old MCP requests."""
    import json

    from daimon.core.turn.ceiling import TURN_CEILING_S

    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Deploy MCP registered minters") < names.index(
        "Refresh worker VM containers (recreate in place)"
    )
    assert names.index("Wait for notebook-host VM refresh") < names.index(
        "Drain legacy MCP turns before first verifier cutover"
    )
    clock = tmp_path / "clock"
    clock.write_text("1000")
    env_rows = [{"name": "UNRELATED_SECRET", "value": "do-not-print"}]
    if state in {"repeat", "resume-live", "resume-expired", "missing-cutoff"}:
        env_rows += [
            {
                "name": "DAIMON_MCP__ALLOW_LEGACY_TOKENS",
                "value": "false" if state == "repeat" else "true",
            },
            {
                "name": "DAIMON_MCP__LEGACY_TOKEN_CUTOFF",
                "value": "2026-10-16T05:00:00Z"
                if state == "resume-live"
                else "2026-10-16T00:00:00Z",
            },
        ]
    if state == "missing-cutoff":
        env_rows = [row for row in env_rows if row["name"] != "DAIMON_MCP__LEGACY_TOKEN_CUTOFF"]
    receipt = {
        "spec": {"template": {"spec": {"timeoutSeconds": 300, "containers": [{"env": env_rows}]}}}
    }
    (tmp_path / "service.json").write_text(json.dumps(receipt))
    commands = {
        "gcloud": '#!/bin/sh\nif [ "$3" = describe ]; then [ "$STATE" != missing-service ] || exit 1; cat "$RUNNER_TEMP/service.json"; else echo "$*" >> "$RUNNER_TEMP/updates"; fi\n',
        "date": '#!/bin/sh\nif [ "$1" = +%s ]; then cat "$RUNNER_TEMP/clock"; elif [ "$3" = "+5 hours" ]; then echo 2026-10-16T05:00:00Z; elif [ "$3" = "2026-10-16T05:00:00Z" ]; then echo 19000; elif [ "$3" = "2026-10-16T00:00:00Z" ]; then echo 500; else echo 2026-10-16T01:00:00Z; fi\n',
        "sleep": '#!/bin/sh\necho "$(( $(cat "$RUNNER_TEMP/clock") + $1 ))" > "$RUNNER_TEMP/clock"\n',
    }
    for command, body in commands.items():
        executable = tmp_path / command
        executable.write_text(body)
        executable.chmod(0o700)
    output = tmp_path / "outputs"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "REGION": "offline",
        "STATE": state,
    }

    def run(step_id: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        script = next(step["run"] for step in steps if step.get("id") == step_id)
        script = script.replace("${{ vars.GCP_AR_REPO }}", "offline").replace(
            "${{ steps.images.outputs.daimon }}", "offline/image"
        )
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    first = run("mcp_minters", env)
    assert "do-not-print" not in first.stdout + first.stderr
    if state in {"missing-service", "missing-cutoff"}:
        assert first.returncode != 0 and not (tmp_path / "updates").exists()
        return
    assert first.returncode == 0, first.stderr
    strict = state in {"repeat", "resume-expired"}
    assert output.read_text() == ("window=false\n" if strict else "window=true\n")
    updates = (tmp_path / "updates").read_text()
    assert "ALLOW_LEGACY_TOKENS=" + ("false" if strict else "true") in updates
    if state == "resume-live":
        assert "LEGACY_TOKEN_CUTOFF=2026-10-16T05:00:00Z" in updates
        assert "+5 hours" not in first.stdout
    if state != "missing-minters":
        (tmp_path / "workers-refreshed-at").write_text("1100")
    second = run("mcp_cutover", {**env, "LEGACY_WINDOW": "false" if strict else "true"})
    assert "do-not-print" not in second.stdout + second.stderr
    if state == "missing-minters":
        assert second.returncode != 0
        assert len((tmp_path / "updates").read_text().splitlines()) == 1
    elif state in {"first", "resume-live"}:
        assert second.returncode == 0, second.stderr
        assert int(clock.read_text()) >= 1000 + 300 + TURN_CEILING_S + 60
        assert "ALLOW_LEGACY_TOKENS=false" in (tmp_path / "updates").read_text().splitlines()[-1]
    else:
        assert second.returncode == 0 and clock.read_text() == "1000"
        assert len((tmp_path / "updates").read_text().splitlines()) == 1


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_only_production_mcp_deploys_pin_the_old_turn_behaviour(
    tmp_path: Path, environment: str
) -> None:
    """Prod keeps today's stops until a promote is approved; staging runs code defaults."""
    import json

    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    step = next(step for step in steps if step.get("id") == "mcp_minters")
    assert step["env"]["DEPLOY_ENVIRONMENT"] == "${{ inputs.environment }}"
    receipt = {"spec": {"template": {"spec": {"timeoutSeconds": 300, "containers": [{}]}}}}
    (tmp_path / "service.json").write_text(json.dumps(receipt))
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(
        '#!/bin/sh\nif [ "$3" = describe ]; then cat "$RUNNER_TEMP/service.json"; '
        'else echo "$*" >> "$RUNNER_TEMP/updates"; fi\n'
    )
    gcloud.chmod(0o700)
    script = (
        step["run"]
        .replace("${{ vars.GCP_AR_REPO }}", "offline")
        .replace("${{ steps.images.outputs.daimon }}", "offline/image")
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(tmp_path / "outputs"),
            "REGION": "offline",
            "DEPLOY_ENVIRONMENT": environment,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    updates = (tmp_path / "updates").read_text()
    pins = (
        "DAIMON_TURN__PATH=mux",
        "DAIMON_TURN__CHANNEL_BACKENDS=false",
        "DAIMON_TURN__AGENT_BACKENDS=false",
    )
    if environment == "production":
        assert all(pin in updates for pin in pins), updates
    else:
        assert "TURN__" not in updates and "FLEET__" not in updates, updates


def test_production_worker_refresh_pins_mux_with_backends_off() -> None:
    """The prod worker env gets mux with both backend switches off after every refresh."""
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Refresh worker VM containers (recreate in place)"
    )
    refresh = script.index("sudo /usr/local/bin/daimon-refresh-env")
    pin = script.index('if [ "${{ inputs.environment }}" = production ]; then', refresh)
    assert pin < script.index("docker compose -f compose.worker.yml pull"), "before recreate"
    block = script[pin : script.index("fi", pin)]
    for line in (
        "DAIMON_TURN__PATH=mux",
        "DAIMON_TURN__CHANNEL_BACKENDS=false",
        "DAIMON_TURN__AGENT_BACKENDS=false",
    ):
        assert line in block
    assert "tee -a /etc/daimon/daimon.env" in block


def test_mux_leg_is_a_blocking_ratchet_inside_the_deploy_gate() -> None:
    """The mux leg selects mux through the environment and ratchets its known failures."""
    jobs = _workflow("ci.yml")["jobs"]
    job = jobs["pytest-mux-leg"]
    assert not job.get("continue-on-error")
    assert job["env"]["DAIMON_TURN__PATH"] == "mux"
    assert job["env"]["DAIMON_TURN__CHANNEL_BACKENDS"] == "true"
    assert all("-p daimon.testing.mux_leg_known" in c for c in _pytest_commands(job))
    # Production defaults to mux, so a mux failure holds back the staging deploy.
    assert "pytest-mux-leg" in jobs["deploy-gcp"]["needs"]
    assert (_ROOT / "tests" / "mux_leg_known_failures.txt").is_file()
    # The other legs never load the ratchet or select a path: the root addopts run
    # them on the emergency-stop leg (daimon.testing.stop_leg_default).
    addopts = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]["pytest"][
        "ini_options"
    ]["addopts"]
    assert "-p daimon.testing.stop_leg_default" in addopts
    for name in ("pytest-db", "pytest-discord"):
        assert "mux_leg_known" not in " ".join(_pytest_commands(jobs[name]))
        assert "DAIMON_TURN__PATH" not in jobs[name]["env"]


def test_staging_configuration_matrix_blocks_deploy_and_runs_real_paths() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    job = jobs["pytest-staging-config"]
    assert "pytest-staging-config" in jobs["deploy-gcp"]["needs"]
    assert not job.get("continue-on-error")
    legs = job["strategy"]["matrix"]["include"]
    assert legs == [
        {
            "configuration": "enabled",
            "path": "mux",
            "channel_backends": "true",
            "agent_backends": "true",
        },
        {
            "configuration": "mux-only",
            "path": "mux",
            "channel_backends": "false",
            "agent_backends": "false",
        },
        {
            "configuration": "legacy-off",
            "path": "legacy",
            "channel_backends": "false",
            "agent_backends": "false",
        },
    ]
    for flag, matrix_key in (
        ("DAIMON_TURN__PATH", "path"),
        ("DAIMON_TURN__CHANNEL_BACKENDS", "channel_backends"),
        ("DAIMON_TURN__AGENT_BACKENDS", "agent_backends"),
        ("DAIMON_CI_CONFIGURATION", "configuration"),
    ):
        assert job["env"][flag] == f"${{{{ matrix.{matrix_key} }}}}"
    command = " ".join(_pytest_commands(job))
    for selected in (
        "test_turn_runs_on_the_selected_leg",
        "packages/core/tests/turn/test_staging_configuration_paths.py",
    ):
        assert selected in command


_CLEAR_STEP = "Forget the deployed commit until this deploy passes"


@pytest.mark.parametrize("removable", [True, False])
def test_staging_forgets_its_deployed_commit_before_any_change(
    tmp_path: Path, removable: bool
) -> None:
    """A deploy that stops midway leaves no record, so the next one is full."""
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Plan the components to refresh") < names.index(_CLEAR_STEP)
    assert names.index(_CLEAR_STEP) < names.index("Run migrations (blocking gate)")
    step = steps[names.index(_CLEAR_STEP)]
    assert without_freshness(step["if"]) == "inputs.environment == 'staging'"
    state = tmp_path / "deployed-sha"
    state.write_text("a" * 40)
    fake = (
        'gcloud() { case "$*" in *describe*) cat "$STATE" ;; '
        '*remove-metadata*) [ "$REMOVABLE" = 1 ] && : > "$STATE" ;; esac; }\n'
        "sleep() { :; }\n"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", fake + step["run"]],
        env={
            **os.environ,
            "STATE": str(state),
            "REMOVABLE": str(int(removable)),
            "WORKERS_VM": "vm",
            "ZONE": "zone-a",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is removable, result.stderr
    assert (state.read_text() == "") is removable


def test_a_config_change_recreates_and_checks_every_worker() -> None:
    steps = {step.get("name"): step for step in _workflow("deploy.yml")["jobs"]["deploy"]["steps"]}
    run = steps["Refresh worker VM containers (recreate in place)"]["run"]
    marker = "config changed: recreating every service"
    assert run.index("before=$(config)") < run.index("daimon-refresh-env")
    assert run.index(f'echo "{marker}"') < run.index("wants() {")
    assert f'grep -q "^{marker}"' in run
    reconcile = "steps.refresh.outputs.reconcile || steps.plan.outputs.reconcile"
    settle = steps["Let the adapters' boot reconcile settle"]
    assert settle["env"]["SERVICES"] == f"${{{{ {reconcile} }}}}"
    for step in (settle, steps["Verify deployed installs match shipped defaults"]):
        assert without_freshness(step["if"]) == f"({reconcile}) != ''"
    assert "steps.refresh.outputs.smoke == 'true'" in steps["Post-deploy smoke turn"]["if"]

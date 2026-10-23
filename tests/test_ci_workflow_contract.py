"""Keep the CI verdict and image-promotion gates linked as workflows evolve."""

import glob
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_ROOT = _WORKFLOWS.parents[1]


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def _pytest_commands(job: dict[str, Any]) -> list[str]:
    """Every `uv run pytest` command of a job, once per matrix leg with the leg's values filled in."""
    legs: list[dict[str, Any]] = job.get("strategy", {}).get("matrix", {}).get("include") or [{}]
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
    legs = jobs["pytest-db"]["strategy"]["matrix"]["include"]
    discord_legs = jobs["pytest-discord"]["strategy"]["matrix"]["include"]
    for suite, count in (("core", 6), ("mcp", 2), ("discord", 2)):
        suite_legs = [leg for leg in legs + discord_legs if leg["suite"] == suite]
        shards = [leg["shard"] for leg in suite_legs]
        assert shards == list(range(count)), f"{suite} must run every shard exactly once"
        assert {leg["shards"] for leg in suite_legs} == {count}
    # The sharded legs of one suite select the same paths, or a shard would skip tests.
    for suite in {leg["suite"] for leg in legs + discord_legs}:
        assert len({leg["paths"] for leg in legs + discord_legs if leg["suite"] == suite}) == 1
    assert {leg["suite"] for leg in legs} == {"core", "mcp", "slack", "teams", "parity"}
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
        "pytest-db",
        "pytest-discord",
        "pytest-light",
        "mux-standalone",
        "pytest-notebook-host",
        "pytest-staging-config",
        "docker-publish-candidate",
    }
    assert {leg["suite"] for leg in jobs["pytest-light"]["strategy"]["matrix"]["include"]} == {
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
    }
    for name, job in pytest_jobs.items():
        for command in _pytest_commands(job):
            assert "-n auto" not in command
            if name in ("pytest-discord", "pytest-mux-leg", "pytest-staging-config"):
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
    legs = job["strategy"]["matrix"]["include"]
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
    with_shards = {
        name
        for name, job in jobs.items()
        if any(
            "shards" in leg for leg in job.get("strategy", {}).get("matrix", {}).get("include", [])
        )
    }
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
    assert deploy[promotable]["if"] == "steps.images.outputs.prebuilt == 'true'"


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
    for step in steps:
        if step.get("name", "").startswith("Build + push"):
            assert step["if"] == "steps.images.outputs.prebuilt != 'true'"
    promote = _workflow("promote.yml")["jobs"]
    assert promote["deploy"]["needs"] == "validate"
    assert promote["deploy"]["with"]["environment"] == "production"
    assert "daimon_digest" not in promote["deploy"]["with"]


@pytest.mark.parametrize(
    ("environment", "digests", "prebuilt"),
    [
        ("staging", ("", "", ""), False),
        ("staging", ("sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64), True),
        ("staging", ("sha256:" + "a" * 64, "", ""), None),
        ("staging", ("sha256:" + "A" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64), None),
        (
            "staging",
            ("sha256:" + "a" * 64 + "\nextra", "sha256:" + "b" * 64, "sha256:" + "c" * 64),
            None,
        ),
        ("production", ("sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64), None),
    ],
)
def test_image_selection_shell_validation(
    tmp_path: Path,
    environment: str,
    digests: tuple[str, str, str],
    prebuilt: bool | None,
) -> None:
    """Execute the workflow's Bash, including its full-value digest check."""
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "images")
    for expression, value in {
        "${{ inputs.environment }}": environment,
        "${{ vars.GCP_PROJECT_ID }}": "test-project",
        "${{ steps.tags.outputs.daimon }}": "example/daimon:abc1234",
        "${{ steps.tags.outputs.notebook }}": "example/notebook-host:abc1234",
        "${{ steps.tags.outputs.report }}": "example/report-host:abc1234",
    }.items():
        script = script.replace(expression, value)
    output = tmp_path / "outputs"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "AR_HOST": "example.test",
        "REPO": "images",
        "DAIMON_DIGEST": digests[0],
        "NOTEBOOK_DIGEST": digests[1],
        "REPORT_DIGEST": digests[2],
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", "gcloud() { :; }\n" + script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if prebuilt is None:
        assert result.returncode != 0
        assert not output.exists() or "prebuilt=true" not in output.read_text()
    else:
        assert result.returncode == 0, result.stderr
        lines = output.read_text().splitlines()
        assert f"prebuilt={str(prebuilt).lower()}" in lines
        if prebuilt:
            assert f"daimon=example.test/test-project/images/daimon@{digests[0]}" in lines
        else:
            assert "daimon=example/daimon:abc1234" in lines


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


def test_mux_leg_is_a_blocking_ratchet_outside_the_deploy_gate() -> None:
    """The mux leg selects mux through the environment and ratchets its known failures."""
    jobs = _workflow("ci.yml")["jobs"]
    job = jobs["pytest-mux-leg"]
    assert not job.get("continue-on-error")
    assert job["env"]["DAIMON_TURN__PATH"] == "mux"
    assert job["env"]["DAIMON_TURN__CHANNEL_BACKENDS"] == "true"
    assert all("-p daimon.testing.mux_leg_known" in c for c in _pytest_commands(job))
    # Production defaults to legacy, so a mux failure must not hold back the staging deploy.
    assert "pytest-mux-leg" not in jobs["deploy-gcp"]["needs"]
    assert (_ROOT / "tests" / "mux_leg_known_failures.txt").is_file()
    # The legacy legs never load the ratchet or select mux.
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
            "fleet_enabled": "true",
        },
        {
            "configuration": "legacy-off",
            "path": "legacy",
            "channel_backends": "false",
            "agent_backends": "false",
            "fleet_enabled": "false",
        },
    ]
    for flag, matrix_key in (
        ("DAIMON_TURN__PATH", "path"),
        ("DAIMON_TURN__CHANNEL_BACKENDS", "channel_backends"),
        ("DAIMON_TURN__AGENT_BACKENDS", "agent_backends"),
        ("DAIMON_FLEET__ENABLED", "fleet_enabled"),
        ("DAIMON_CI_CONFIGURATION", "configuration"),
    ):
        assert job["env"][flag] == f"${{{{ matrix.{matrix_key} }}}}"
    command = " ".join(_pytest_commands(job))
    for selected in (
        "test_turn_runs_on_the_selected_leg",
        "packages/core/tests/turn/test_staging_configuration_paths.py",
    ):
        assert selected in command

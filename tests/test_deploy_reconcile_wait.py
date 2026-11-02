"""Execute the deploy's boot-reconcile wait offline, both halves.

The wait may end before its fixed floor (90s after the worker refresh) only
when every running container that reconciles at boot has logged its own
completion event. A failure event, a service with no completion event, or any
error falls back to the floor, and nothing ever waits past it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_STEP = "Let the adapters' boot reconcile settle"
_IMAGE = "example.test/p/r/daimon@sha256:" + "a" * 64
_STARTED = "2026-10-19T05:21:51.123456789Z"
_START = 10_000


def _step() -> dict[str, Any]:
    steps = yaml.safe_load((_ROOT / ".github/workflows/deploy.yml").read_text())["jobs"]["deploy"][
        "steps"
    ]
    return next(step for step in steps if step.get("name") == _STEP)


def _remote_script() -> str:
    run = _step()["run"]
    start = run.index("<<'REMOTE'\n") + len("<<'REMOTE'\n")
    return run[start : run.index("\nREMOTE\n", start)]


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o700)


def _clock_tools(bin_dir: Path) -> None:
    _executable(
        bin_dir / "date",
        'if [ "$1" = +%s ]; then cat "$FAKE/clock"; else exec /bin/date "$@"; fi\n',
    )
    _executable(
        bin_dir / "sleep",
        'echo "$1" >> "$FAKE/sleeps"\necho "$(( $(cat "$FAKE/clock") + $1 ))" > "$FAKE/clock"\n',
    )


# --- the remote loop -------------------------------------------------------


def _container(
    status: str = "running",
    *,
    code: int = 0,
    policy: str = "unless-stopped",
    image: str = _IMAGE,
    logs: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {"state": f"{status} {code} {policy} {_STARTED} {image}", "logs": logs}


def _event(name: str) -> str:
    return f'{{"tenants": 2, "event": "{name}", "level": "info"}}'


def _run_remote(
    tmp_path: Path,
    containers: dict[str, list[dict[str, Any]]],
    *,
    budget: int = 20,
    failing_ps: str | None = None,
    services: str = "discord slack teams",
) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "fake"
    bin_dir = tmp_path / "bin"
    fake.mkdir()
    bin_dir.mkdir()
    (fake / "clock").write_text(str(_START))
    for service in ("discord", "slack", "teams"):
        ids: list[str] = []
        for index, container in enumerate(containers.get(service, [])):
            cid = f"{service}{index}"
            ids.append(cid)
            (fake / f"inspect-{cid}").write_text(container["state"] + "\n")
            (fake / f"logs-{cid}").write_text("".join(f"{line}\n" for line in container["logs"]))
        (fake / f"ps-{service}").write_text("".join(f"{cid}\n" for cid in ids))
    if failing_ps is not None:
        (fake / f"ps-{failing_ps}").unlink()
    _clock_tools(bin_dir)
    _executable(bin_dir / "sudo", '[ "$1" = -E ] && shift\nexec "$@"\n')
    _executable(
        bin_dir / "docker",
        "\n".join(
            [
                'case "$1" in',
                '  compose) eval "svc=\\${$#}"; cat "$FAKE/ps-$svc" ;;',
                '  inspect) eval "id=\\${$#}"; cat "$FAKE/inspect-$id" ;;',
                '  logs) echo "$*" >> "$FAKE/logs-args"; eval "id=\\${$#}";'
                ' cat "$FAKE/logs-$id"; echo "stderr noise" >&2 ;;',
                "  *) exit 99 ;;",
                "esac",
                "",
            ]
        ),
    )
    script = _remote_script().replace("cd /etc/daimon", f'cd "{tmp_path}"')
    return subprocess.run(
        ["sh", "-c", f'budget={budget} image={_IMAGE} services="{services}"\n{script}'],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE": str(fake)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _clock(tmp_path: Path) -> int:
    return int((tmp_path / "fake" / "clock").read_text())


_SLACK_DONE = _container(logs=(_event("slack.boot_sweep_complete"),))
_TEAMS_DISABLED = _container("exited", code=0, policy="on-failure")


def test_every_completion_event_ends_the_wait_at_once(tmp_path: Path) -> None:
    result = _run_remote(tmp_path, {"slack": [_SLACK_DONE], "teams": [_TEAMS_DISABLED]})
    assert result.returncode == 0, result.stdout + result.stderr
    assert _clock(tmp_path) == _START
    # Only this run of the container counts: logs are read from its start.
    assert f"--since {_STARTED}" in (tmp_path / "fake" / "logs-args").read_text()


def test_teams_ready_and_skipped_sweep_count_as_complete(tmp_path: Path) -> None:
    result = _run_remote(
        tmp_path,
        {
            "slack": [_container(logs=(_event("slack.boot_sweep_skipped"),))],
            "teams": [_container(policy="on-failure", logs=(_event("teams.tenant_ready"),))],
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


_DISCORD_DONE = _container(logs=(_event("discord.boot_reconcile_complete"),))


def test_discord_completion_ends_the_wait_with_the_others(tmp_path: Path) -> None:
    result = _run_remote(
        tmp_path, {"discord": [_DISCORD_DONE], "slack": [_SLACK_DONE], "teams": [_TEAMS_DISABLED]}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _clock(tmp_path) == _START


@pytest.mark.parametrize(
    "logs",
    [
        (),  # an image from before the event: never logged
        ("discord.boot_reconcile_failed",),
        ("discord.boot_reconcile_complete", "discord.boot_reconcile_failed"),
    ],
)
def test_discord_without_a_clean_completion_keeps_the_fixed_wait(
    tmp_path: Path, logs: tuple[str, ...]
) -> None:
    discord = _container(logs=tuple(_event(name) for name in logs))
    result = _run_remote(
        tmp_path, {"discord": [discord], "slack": [_SLACK_DONE], "teams": [_TEAMS_DISABLED]}
    )
    assert result.returncode == 3, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("service", "logs"),
    [
        ("slack", ("slack.boot_sweep_crashed",)),
        ("slack", ("slack.boot_sweep_tenant_failed", "slack.boot_sweep_complete")),
        ("slack", ("slack.boot_sweep_complete", "slack.boot_sweep_crashed")),
        ("teams", ("teams.tenant_reconcile_failed",)),
        ("teams", ("teams.task_failed", "teams.tenant_ready")),
    ],
)
def test_a_sweep_failure_event_is_never_complete(
    tmp_path: Path, service: str, logs: tuple[str, ...]
) -> None:
    containers = {"slack": [_SLACK_DONE], "teams": [_TEAMS_DISABLED]}
    containers[service] = [_container(logs=tuple(_event(name) for name in logs))]
    result = _run_remote(tmp_path, containers)
    assert result.returncode == 3
    assert f"{service} logged" in result.stdout


@pytest.mark.parametrize(
    "containers",
    [
        # An image from before the event, or a sweep still running.
        {"slack": [_container(logs=(_event("slack.boot_sweep_started"),))]},
        # The event named in a message, not as the event, does not count.
        {"slack": [_container(logs=('{"event": "x", "note": "slack.boot_sweep_complete"}',))]},
        # A crashed teams container restarts (on-failure) and reconciles again.
        {"slack": [_SLACK_DONE], "teams": [_container("exited", code=1, policy="on-failure")]},
        {"slack": [_SLACK_DONE], "teams": [_container("restarting", policy="on-failure")]},
        # unless-stopped restarts even a clean exit.
        {"slack": [_container("exited", code=0)]},
    ],
)
def test_without_completion_it_polls_until_the_budget_and_falls_back(
    tmp_path: Path, containers: dict[str, list[dict[str, Any]]]
) -> None:
    result = _run_remote(tmp_path, containers, budget=21)
    assert result.returncode == 3
    assert "still waiting on" in result.stdout
    sleeps = (tmp_path / "fake" / "sleeps").read_text().split()
    assert set(sleeps) == {"2"}
    assert _clock(tmp_path) - _START <= 21 + 1


@pytest.mark.parametrize(
    ("containers", "failing_ps", "message"),
    [
        ({"slack": [_container(image="example.test/old:abc1234")]}, None, "slack runs"),
        ({"slack": [_SLACK_DONE]}, "teams", "cannot list teams"),
    ],
)
def test_unexpected_state_falls_back(
    tmp_path: Path,
    containers: dict[str, list[dict[str, Any]]],
    failing_ps: str | None,
    message: str,
) -> None:
    result = _run_remote(tmp_path, containers, failing_ps=failing_ps)
    assert result.returncode == 3
    assert message in result.stdout


def test_only_the_restarted_services_are_waited_on(tmp_path: Path) -> None:
    # Discord was not restarted, so its older image and missing event are fine.
    containers = {"discord": [_container(image="example.test/old:abc1234")], "slack": [_SLACK_DONE]}
    result = _run_remote(tmp_path, containers, services="slack")
    assert result.returncode == 0, result.stdout


# --- the runner half -------------------------------------------------------


def _run_step(
    tmp_path: Path,
    *,
    refreshed_ago: int | None = 20,
    ssh_rc: int = 0,
    ssh_takes: int = 0,
    image: str = _IMAGE,
    services: str = "discord slack teams",
) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "fake"
    bin_dir = tmp_path / "bin"
    fake.mkdir()
    bin_dir.mkdir()
    (fake / "clock").write_text(str(_START))
    if refreshed_ago is not None:
        (tmp_path / "workers-refreshed-at").write_text(str(_START - refreshed_ago))
    _clock_tools(bin_dir)
    _executable(bin_dir / "timeout", 'echo "$1" > "$FAKE/timeout"\nshift\nexec "$@"\n')
    _executable(
        bin_dir / "gcloud",
        "\n".join(
            [
                'eval "payload=\\${$#}"',
                'printf "%s" "$payload" > "$FAKE/payload"',
                'echo "$(( $(cat "$FAKE/clock") + SSH_TAKES ))" > "$FAKE/clock"',
                'exit "$SSH_RC"',
                "",
            ]
        ),
    )
    script = _step()["run"].replace("${{ vars.GCP_AR_REPO }}", "offline")
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE": str(fake),
            "RUNNER_TEMP": str(tmp_path),
            "ZONE": "offline",
            "DAIMON_IMAGE": image,
            "SERVICES": services,
            "SSH_RC": str(ssh_rc),
            "SSH_TAKES": str(ssh_takes),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


_DEADLINE = _START - 20 + 90


def test_reported_completion_skips_the_rest_of_the_floor(tmp_path: Path) -> None:
    result = _run_step(tmp_path, ssh_rc=0, ssh_takes=4)
    assert result.returncode == 0, result.stderr
    assert _clock(tmp_path) == _START + 4
    remaining = _DEADLINE - _START
    assert int((tmp_path / "fake" / "timeout").read_text()) == remaining
    payload = (tmp_path / "fake" / "payload").read_text()
    assert payload.startswith(
        f'budget={remaining - 10} image={_IMAGE} services="discord slack teams"\n'
    )
    assert payload.endswith(_remote_script())


@pytest.mark.parametrize(
    ("ssh_rc", "ssh_takes"),
    [
        (3, 5),  # no completion (or a failure event): the floor
        (3, 60),  # the remote loop ran out its budget
        (255, 1),  # SSH failed
        (124, 70),  # `timeout` cut the SSH at the deadline
    ],
)
def test_any_other_outcome_waits_exactly_to_the_old_deadline(
    tmp_path: Path, ssh_rc: int, ssh_takes: int
) -> None:
    result = _run_step(tmp_path, ssh_rc=ssh_rc, ssh_takes=ssh_takes)
    assert result.returncode == 0, result.stderr
    assert _clock(tmp_path) == _DEADLINE


@pytest.mark.parametrize(
    ("refreshed_ago", "image", "ssh_expected"),
    [
        (85, _IMAGE, False),  # too little left to be worth an SSH
        (20, "bad'image", False),  # never interpolate an unexpected image ref
        (95, _IMAGE, False),  # past the deadline already
        (None, _IMAGE, False),  # workers VM stopped: no refresh, no wait
    ],
)
def test_short_or_skipped_waits_never_pass_the_deadline(
    tmp_path: Path, refreshed_ago: int | None, image: str, ssh_expected: bool
) -> None:
    result = _run_step(tmp_path, refreshed_ago=refreshed_ago, image=image)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "fake" / "payload").exists() is ssh_expected
    if refreshed_ago is None:
        assert _clock(tmp_path) == _START
    else:
        assert _clock(tmp_path) == max(_START, _START - refreshed_ago + 90)


def test_an_unexpected_service_list_is_never_interpolated(tmp_path: Path) -> None:
    result = _run_step(tmp_path, services="slack; true")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "fake" / "payload").exists()
    assert _clock(tmp_path) == _DEADLINE

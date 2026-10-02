"""Local setup and Discord preflight contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.cli.commands import setup as setup_mod
from daimon.adapters.cli.main import app
from typer.testing import CliRunner


def _invoke(*args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(app, ["setup", *args])
    assert result.stdout.strip(), repr(result.exception)
    return result.exit_code, json.loads(result.stdout)


def _env_values(content: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in content.splitlines() if "=" in line)


_PERMISSION_BITS = sum(1 << bit for bit in (10, 11, 14, 16, 34, 35, 38))


def test_setup_generates_valid_secrets_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "DAIMON_ANTHROPIC__API_KEY",
        "DAIMON_DISCORD__BOT_TOKEN",
        "DAIMON_MCP__PUBLIC_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    rc, payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert set(payload) == {
        "schema_version",
        "status",
        "completed",
        "missing",
        "next_step",
        "next_optional",
    }
    assert payload["schema_version"] == 1
    assert payload["status"] == "needs_input"
    assert payload["next_optional"] == []
    assert payload["missing"] == ["DAIMON_ANTHROPIC__API_KEY"]
    assert (
        payload["next_step"]
        == "Set DAIMON_ANTHROPIC__API_KEY in .env to a key from a dedicated Anthropic workspace."
    )
    values = _env_values(env_file.read_text())
    assert len(values["POSTGRES_PASSWORD"]) >= 40
    assert all(char.isalnum() or char in "_-" for char in values["POSTGRES_PASSWORD"])
    assert len(values["DAIMON_MCP__JWT_SECRET"]) >= 40
    Fernet(values["DAIMON_CRYPTO__KEYS"].encode())
    assert values["DAIMON_DATABASE__URL"].endswith(
        f":{values['POSTGRES_PASSWORD']}@localhost:5432/daimon"
    )
    assert values["DAIMON_MCP__PUBLIC_URL"] == "http://localhost:8765/mcp"
    assert os.stat(env_file).st_mode & 0o777 == 0o600
    for value in values.values():
        assert value not in json.dumps(payload)


def test_setup_is_idempotent_and_preserves_existing_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=existing_password\nDAIMON_ANTHROPIC__API_KEY=existing_api_key\n"
    )
    assert _invoke("--env-file", str(env_file))[0] == 0
    first = env_file.read_bytes()
    rc, payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert env_file.read_bytes() == first
    assert _env_values(first.decode())["POSTGRES_PASSWORD"] == "existing_password"
    assert "DAIMON_ANTHROPIC__API_KEY" not in payload["missing"]


def test_setup_rejects_symlink(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.write_text("POSTGRES_PASSWORD=kept\n")
    alias = tmp_path / ".env"
    alias.symlink_to(actual)
    rc, payload = _invoke("--env-file", str(alias))
    assert rc == 1
    assert actual.read_text() == "POSTGRES_PASSWORD=kept\n"
    assert payload["completed"] == []
    assert payload["status"] == "error"


def test_stdlib_bootstrap_matches_installed_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    script = Path(__file__).resolve().parents[5] / "scripts/setup.py"
    environment = os.environ.copy()
    for name in (
        "DAIMON_ANTHROPIC__API_KEY",
        "DAIMON_MCP__PUBLIC_URL",
        "DAIMON_DISCORD__BOT_TOKEN",
    ):
        environment.pop(name, None)
        monkeypatch.delenv(name, raising=False)
    first = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--env-file", str(env_file)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    before = env_file.read_bytes()
    script_payload = json.loads(first.stdout)
    rc, cli_payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert env_file.read_bytes() == before
    assert (
        set(script_payload)
        == set(cli_payload)
        == {"schema_version", "status", "completed", "missing", "next_step", "next_optional"}
    )
    assert script_payload["missing"] == cli_payload["missing"]
    assert script_payload["next_step"] == cli_payload["next_step"]
    assert script_payload["next_optional"] == cli_payload["next_optional"]
    assert first.stderr == ""
    for value in _env_values(before.decode()).values():
        assert value not in first.stdout


def test_ready_step_names_seeded_cli_first_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "DAIMON_ANTHROPIC__API_KEY",
        "DAIMON_MCP__PUBLIC_URL",
        "DAIMON_DISCORD__BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DAIMON_ANTHROPIC__API_KEY=test-key\n"
        "DAIMON_MCP__PUBLIC_URL=http://localhost:8765/mcp\n"
        "DAIMON_DISCORD__BOT_TOKEN=test-token\n"
    )
    rc, payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert payload["missing"] == []
    assert payload["status"] == "ready"
    assert payload["next_optional"] == [
        {
            "id": "github_app",
            "command": "daimon github register-app --org <org> --origin <url> --json",
            "why": "let agents read and open PRs on your repos",
            "status": "planned",
            "available": False,
        }
    ]
    assert (
        "docker compose run --rm --no-deps --entrypoint daimon init sessions create --json"
        in payload["next_step"]
    )
    assert (
        "docker compose run --rm --no-deps --entrypoint daimon init run --session ID"
        in payload["next_step"]
    )
    assert "test-key" not in json.dumps(payload)


def test_cli_first_reply_step_precedes_optional_discord_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "DAIMON_ANTHROPIC__API_KEY",
        "DAIMON_MCP__PUBLIC_URL",
        "DAIMON_DISCORD__BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DAIMON_ANTHROPIC__API_KEY=test-key\nDAIMON_MCP__PUBLIC_URL=http://localhost:8765/mcp\n"
    )
    rc, payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert payload["missing"] == []
    assert payload["status"] == "ready"
    assert payload["next_optional"][0]["available"] is False
    assert "docker compose up --build -d postgres init" in payload["next_step"]
    assert "sessions create --json" in payload["next_step"]


def _mock_discord(monkeypatch: pytest.MonkeyPatch, responses: dict[str, tuple[int, Any]]) -> None:
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = responses.get(request.url.path, (404, {}))
        assert request.headers["Authorization"] == "Bot test-token"
        return httpx.Response(status, json=body)

    def client_factory(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(setup_mod.httpx, "Client", client_factory)


def _discord_env(tmp_path: Path) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text("DAIMON_DISCORD__BOT_TOKEN=test-token\n")
    return env_file


def test_discord_verification_passes_all_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAIMON_DISCORD__BOT_TOKEN", raising=False)
    env_file = _discord_env(tmp_path)
    bits = _PERMISSION_BITS
    _mock_discord(
        monkeypatch,
        {
            "/api/v10/users/@me": (200, {"id": "123", "bot": True}),
            "/api/v10/oauth2/applications/@me": (200, {"flags": 1 << 19}),
            "/api/v10/guilds/456/members/123": (200, {"roles": ["789"]}),
            "/api/v10/guilds/456/roles": (
                200,
                [{"id": "456", "permissions": "0"}, {"id": "789", "permissions": str(bits)}],
            ),
        },
    )
    rc, payload = _invoke("verify", "discord", "--env-file", str(env_file), "--guild-id", "456")
    assert rc == 0
    assert set(payload) == {
        "schema_version",
        "status",
        "completed",
        "missing",
        "failures",
        "warnings",
        "next_step",
    }
    assert payload["schema_version"] == 1
    assert payload["status"] == "passed"
    assert payload["completed"] == [
        "token",
        "message_content_intent_flag",
        "guild_membership",
        "guild_permissions",
    ]
    assert payload["failures"] == []
    assert payload["warnings"] == []
    assert "test-token" not in json.dumps(payload)


_FAILURE_CASES: list[tuple[dict[str, tuple[int, Any]], str]] = [
    ({"/api/v10/users/@me": (401, {})}, "Token check: Discord rejected the bot token"),
    (
        {"/api/v10/oauth2/applications/@me": (200, {"flags": 0})},
        "Message Content Intent is disabled",
    ),
    ({"/api/v10/guilds/456/members/123": (404, {})}, "Guild membership: Discord returned HTTP 404"),
    (
        {"/api/v10/guilds/456/roles": (200, [{"id": "456", "permissions": "0"}])},
        "Missing guild permissions:",
    ),
]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    _FAILURE_CASES,
)
def test_discord_verification_reports_precise_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, tuple[int, Any]],
    expected: str,
) -> None:
    monkeypatch.delenv("DAIMON_DISCORD__BOT_TOKEN", raising=False)
    env_file = _discord_env(tmp_path)
    bits = _PERMISSION_BITS
    responses: dict[str, tuple[int, Any]] = {
        "/api/v10/users/@me": (200, {"id": "123", "bot": True}),
        "/api/v10/oauth2/applications/@me": (200, {"flags": 1 << 19}),
        "/api/v10/guilds/456/members/123": (200, {"roles": ["789"]}),
        "/api/v10/guilds/456/roles": (200, [{"id": "789", "permissions": str(bits)}]),
    }
    responses.update(overrides)
    _mock_discord(monkeypatch, responses)
    rc, payload = _invoke("verify", "discord", "--env-file", str(env_file), "--guild-id", "456")
    assert rc == 1
    assert any(expected in failure for failure in payload["failures"])
    assert "test-token" not in json.dumps(payload)


def test_discord_verification_reports_missing_human_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAIMON_DISCORD__BOT_TOKEN", raising=False)
    rc, payload = _invoke("verify", "discord", "--env-file", str(tmp_path / ".env"))
    assert rc == 1
    assert payload["missing"] == ["DAIMON_DISCORD__BOT_TOKEN", "guild_id"]
    assert payload["next_step"].startswith("Set DAIMON_DISCORD__BOT_TOKEN")


@pytest.mark.parametrize(
    ("verified", "guild_count", "expect_warning"),
    [(True, 100, True), (True, 99, False), (False, 100, False), (None, 100, False)],
)
def test_discord_review_warning_uses_exposed_verified_guild_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool | None,
    guild_count: int,
    expect_warning: bool,
) -> None:
    monkeypatch.delenv("DAIMON_DISCORD__BOT_TOKEN", raising=False)
    env_file = _discord_env(tmp_path)
    application: dict[str, Any] = {
        "flags": 1 << 18,
        "approximate_guild_count": guild_count,
    }
    if verified is not None:
        application["verified"] = verified
    _mock_discord(
        monkeypatch,
        {
            "/api/v10/users/@me": (200, {"id": "123", "bot": True}),
            "/api/v10/oauth2/applications/@me": (200, application),
            "/api/v10/guilds/456/members/123": (200, {"roles": ["789"]}),
            "/api/v10/guilds/456/roles": (
                200,
                [{"id": "789", "permissions": str(_PERMISSION_BITS)}],
            ),
        },
    )
    rc, payload = _invoke("verify", "discord", "--env-file", str(env_file), "--guild-id", "456")
    assert rc == 0
    assert payload["completed"][1] == "message_content_intent_flag"
    assert bool(payload["warnings"]) is expect_warning
    if expect_warning:
        assert "10,000 reachable users" in payload["warnings"][0]
        assert "do not prove approval" in payload["warnings"][0]
    assert "test-token" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("overwrites", "expected_failure"),
    [
        ([], None),
        ([{"id": "456", "type": 0, "allow": "0", "deny": str(1 << 10)}], "view_channel"),
        (
            [
                {"id": "456", "type": 0, "allow": "0", "deny": str(1 << 11)},
                {"id": "789", "type": 0, "allow": str(1 << 11), "deny": "0"},
            ],
            None,
        ),
        (
            [
                {"id": "789", "type": 0, "allow": str(1 << 10), "deny": "0"},
                {"id": "123", "type": 1, "allow": "0", "deny": str(1 << 10)},
            ],
            "view_channel",
        ),
    ],
)
def test_discord_channel_effective_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overwrites: list[dict[str, Any]],
    expected_failure: str | None,
) -> None:
    monkeypatch.delenv("DAIMON_DISCORD__BOT_TOKEN", raising=False)
    env_file = _discord_env(tmp_path)
    _mock_discord(
        monkeypatch,
        {
            "/api/v10/users/@me": (200, {"id": "123", "bot": True}),
            "/api/v10/oauth2/applications/@me": (200, {"flags": 1 << 19}),
            "/api/v10/guilds/456/members/123": (200, {"roles": ["789"]}),
            "/api/v10/guilds/456/roles": (
                200,
                [
                    {"id": "456", "permissions": "0"},
                    {"id": "789", "permissions": str(_PERMISSION_BITS)},
                ],
            ),
            "/api/v10/channels/999": (
                200,
                {"id": "999", "guild_id": "456", "type": 0, "permission_overwrites": overwrites},
            ),
        },
    )
    rc, payload = _invoke(
        "verify", "discord", "--env-file", str(env_file), "--guild-id", "456", "--channel-id", "999"
    )
    if expected_failure is None:
        assert rc == 0
        assert payload["status"] == "passed"
        assert "channel_permissions" in payload["completed"]
    else:
        assert rc == 1
        assert payload["status"] == "failed"
        assert any(expected_failure in failure for failure in payload["failures"])
    assert "test-token" not in json.dumps(payload)


def test_discord_channel_must_belong_to_guild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAIMON_DISCORD__BOT_TOKEN", raising=False)
    env_file = _discord_env(tmp_path)
    _mock_discord(
        monkeypatch,
        {
            "/api/v10/users/@me": (200, {"id": "123", "bot": True}),
            "/api/v10/oauth2/applications/@me": (200, {"flags": 1 << 19}),
            "/api/v10/guilds/456/members/123": (200, {"roles": ["789"]}),
            "/api/v10/guilds/456/roles": (
                200,
                [{"id": "789", "permissions": str(_PERMISSION_BITS)}],
            ),
            "/api/v10/channels/999": (200, {"guild_id": "other", "permission_overwrites": []}),
        },
    )
    rc, payload = _invoke(
        "verify", "discord", "--env-file", str(env_file), "--guild-id", "456", "--channel-id", "999"
    )
    assert rc == 1
    assert "channel belongs to another guild" in payload["next_step"]

"""Local setup and Discord preflight contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.cli.commands import setup as setup_mod
from daimon.adapters.cli.main import app
from daimon.adapters.cli.setup_bootstrap import values_from_env
from daimon.core.ma_identity import derive_tenant_uuid
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
        "optional_actions",
    }
    assert payload["schema_version"] == 1
    assert payload["status"] == "needs_input"
    assert payload["next_optional"] == []
    assert payload["missing"] == ["DAIMON_ANTHROPIC__API_KEY"]
    assert payload["optional_actions"][0]["id"] == "discord"
    assert payload["optional_actions"][0]["status"] == "needs_token"
    assert "DAIMON_DISCORD__BOT_TOKEN" in payload["optional_actions"][0]["next_step"]
    assert (
        payload["next_step"]
        == "Set DAIMON_ANTHROPIC__API_KEY in .env to a key from a dedicated Anthropic workspace."
    )
    values = _env_values(env_file.read_text())
    assert values["DAIMON_CLI__WORKSPACE_ID"].startswith("install-")
    assert derive_tenant_uuid(platform="cli", workspace_id="local") != derive_tenant_uuid(
        platform="cli", workspace_id=values["DAIMON_CLI__WORKSPACE_ID"]
    )
    assert len(values["POSTGRES_PASSWORD"]) >= 40
    assert all(char.isalnum() or char in "_-" for char in values["POSTGRES_PASSWORD"])
    assert len(values["DAIMON_MCP__JWT_SECRET"]) >= 40
    Fernet(values["DAIMON_CRYPTO__KEYS"].encode())
    assert values["DAIMON_DATABASE__URL"].endswith(
        f":{values['POSTGRES_PASSWORD']}@localhost:5432/daimon"
    )
    assert "DAIMON_MCP__PUBLIC_URL" not in values
    assert os.stat(env_file).st_mode & 0o777 == 0o600
    for value in values.values():
        assert value not in json.dumps(payload)


def test_setup_is_idempotent_and_preserves_existing_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=existing_password\n"
        "DAIMON_ANTHROPIC__API_KEY=existing_api_key\n"
        "DAIMON_MCP__PUBLIC_URL=https://mcp.example.test/mcp\n"
    )
    assert _invoke("--env-file", str(env_file))[0] == 0
    first = env_file.read_bytes()
    rc, payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert env_file.read_bytes() == first
    assert _env_values(first.decode())["POSTGRES_PASSWORD"] == "existing_password"
    assert _env_values(first.decode())["DAIMON_MCP__PUBLIC_URL"] == "https://mcp.example.test/mcp"
    assert "DAIMON_ANTHROPIC__API_KEY" not in payload["missing"]


def test_fresh_installs_get_distinct_stable_cli_tenants(tmp_path: Path) -> None:
    first_file = tmp_path / "one.env"
    second_file = tmp_path / "two.env"
    assert _invoke("--env-file", str(first_file))[0] == 0
    assert _invoke("--env-file", str(second_file))[0] == 0
    first_id = _env_values(first_file.read_text())["DAIMON_CLI__WORKSPACE_ID"]
    second_id = _env_values(second_file.read_text())["DAIMON_CLI__WORKSPACE_ID"]
    assert first_id != second_id
    assert derive_tenant_uuid(platform="cli", workspace_id=first_id) != derive_tenant_uuid(
        platform="cli", workspace_id=second_id
    )
    before = first_file.read_bytes()
    assert _invoke("--env-file", str(first_file))[0] == 0
    assert first_file.read_bytes() == before


def test_legacy_install_keeps_cli_local_tenant(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=existing-password\n"
        "DAIMON_MCP__JWT_SECRET=existing-jwt-secret\n"
        "DAIMON_CRYPTO__KEYS=existing-crypto-key\n"
    )
    assert _invoke("--env-file", str(env_file))[0] == 0
    assert _env_values(env_file.read_text())["DAIMON_CLI__WORKSPACE_ID"] == "local"


@pytest.mark.parametrize(
    "existing_line",
    ["POSTGRES_PASSWORD=existing-password", "DAIMON_DISCORD__BOT_TOKEN=existing-token"],
)
def test_partial_legacy_env_keeps_cli_local_tenant(tmp_path: Path, existing_line: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(existing_line + "\n")
    assert _invoke("--env-file", str(env_file))[0] == 0
    assert _env_values(env_file.read_text())["DAIMON_CLI__WORKSPACE_ID"] == "local"


def test_legacy_process_secret_keeps_cli_local_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_MCP__JWT_SECRET", "existing-jwt-secret")
    env_file = tmp_path / ".env"
    assert _invoke("--env-file", str(env_file))[0] == 0
    assert _env_values(env_file.read_text())["DAIMON_CLI__WORKSPACE_ID"] == "local"


def test_untouched_example_and_fresh_api_key_get_unique_tenants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "new-key")
    example = Path(__file__).resolve().parents[5] / ".env.example"
    env_file = tmp_path / ".env"
    env_file.write_text(example.read_text())
    assert _invoke("--env-file", str(env_file))[0] == 0
    assert _env_values(env_file.read_text())["DAIMON_CLI__WORKSPACE_ID"].startswith("install-")
    second = tmp_path / "new.env"
    assert _invoke("--env-file", str(second))[0] == 0
    assert _env_values(second.read_text())["DAIMON_CLI__WORKSPACE_ID"].startswith("install-")


def test_inline_comments_do_not_supply_environment_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DAIMON_ANTHROPIC__API_KEY= # add later\n"
        "DAIMON_DISCORD__BOT_TOKEN= # add later\n"
        "POSTGRES_PASSWORD= # add later\n"
    )
    rc, payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert payload["missing"] == ["DAIMON_ANTHROPIC__API_KEY"]
    assert payload["optional_actions"][0]["status"] == "needs_token"
    assert _env_values(env_file.read_text())["DAIMON_CLI__WORKSPACE_ID"].startswith("install-")


def test_env_parser_preserves_hash_inside_quotes() -> None:
    assert values_from_env('TOKEN="part # secret" # trailing comment\nEMPTY= # later\n') == {
        "TOKEN": "part # secret",
        "EMPTY": "",
    }


def test_setup_preserves_explicit_cli_workspace(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DAIMON_CLI__WORKSPACE_ID=cli-probe\n")
    assert _invoke("--env-file", str(env_file))[0] == 0
    assert _env_values(env_file.read_text())["DAIMON_CLI__WORKSPACE_ID"] == "cli-probe"


def test_compose_forwards_probe_workspace_to_init_and_cli(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is not installed")
    compose_file = Path(__file__).resolve().parents[5] / "docker-compose.yml"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=test-password\n"
        "DAIMON_ANTHROPIC__API_KEY=test-key\n"
        "DAIMON_MCP__JWT_SECRET=test-jwt\n"
        "DAIMON_CLI__WORKSPACE_ID=from-file\n"
    )
    environment = os.environ.copy()
    environment["DAIMON_CLI__WORKSPACE_ID"] = "agent-setup-probe"
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    # `docker compose run init ...` uses this same service environment.
    assert (
        json.loads(result.stdout)["services"]["init"]["environment"]["DAIMON_CLI__WORKSPACE_ID"]
        == "agent-setup-probe"
    )


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
    assert payload["optional_actions"] == []


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
        == {
            "schema_version",
            "status",
            "completed",
            "missing",
            "next_step",
            "next_optional",
            "optional_actions",
        }
    )
    assert script_payload["missing"] == cli_payload["missing"]
    assert script_payload["next_step"] == cli_payload["next_step"]
    assert script_payload["next_optional"] == cli_payload["next_optional"]
    assert script_payload["optional_actions"] == cli_payload["optional_actions"]
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
        "DAIMON_ANTHROPIC__API_KEY=test-key\nDAIMON_DISCORD__BOT_TOKEN=test-token\n"
    )
    rc, payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert payload["missing"] == []
    assert payload["status"] == "ready"
    assert payload["optional_actions"][0]["status"] == "ready_to_verify"
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
    env_file.write_text("DAIMON_ANTHROPIC__API_KEY=test-key\n")
    rc, payload = _invoke("--env-file", str(env_file))
    assert rc == 0
    assert payload["missing"] == []
    assert payload["status"] == "ready"
    assert payload["optional_actions"][0]["status"] == "needs_token"
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
    assert "10,000 reachable users" in payload["warnings"][0]
    assert any("Optional attach_files" in warning for warning in payload["warnings"])
    assert any("Optional add_reactions" in warning for warning in payload["warnings"])
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
    ("verified", "guild_count"),
    [(True, 100), (True, 99), (False, 100), (None, 100)],
)
def test_discord_review_warning_does_not_infer_approval_from_guild_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool | None,
    guild_count: int,
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
    assert "10,000 reachable users" in payload["warnings"][0]
    assert "does not expose that count or review approval" in payload["warnings"][0]
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


def test_channel_overwrite_can_grant_permissions_missing_from_guild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAIMON_DISCORD__BOT_TOKEN", raising=False)
    env_file = _discord_env(tmp_path)
    all_bits = _PERMISSION_BITS | (1 << 6) | (1 << 15)
    _mock_discord(
        monkeypatch,
        {
            "/api/v10/users/@me": (200, {"id": "123", "bot": True}),
            "/api/v10/oauth2/applications/@me": (200, {"flags": 1 << 19}),
            "/api/v10/guilds/456/members/123": (200, {"roles": ["789"]}),
            "/api/v10/guilds/456/roles": (200, [{"id": "456", "permissions": "0"}]),
            "/api/v10/channels/999": (
                200,
                {
                    "guild_id": "456",
                    "permission_overwrites": [
                        {"id": "123", "type": 1, "allow": str(all_bits), "deny": "0"}
                    ],
                },
            ),
        },
    )
    rc, payload = _invoke(
        "verify", "discord", "--env-file", str(env_file), "--guild-id", "456", "--channel-id", "999"
    )
    assert rc == 0
    assert payload["status"] == "passed"
    assert payload["completed"][-1] == "channel_permissions"
    assert not any("Missing guild permissions" in item for item in payload["failures"])
    assert not any("Optional" in item for item in payload["warnings"])

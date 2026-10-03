"""Dependency-free setup core, shared by the CLI and the source checkout script."""

from __future__ import annotations

import base64
import os
import re
import secrets
import stat
from pathlib import Path

_GENERATED = ("POSTGRES_PASSWORD", "DAIMON_MCP__JWT_SECRET", "DAIMON_CRYPTO__KEYS")
_HUMAN_STEPS = (
    (
        "DAIMON_ANTHROPIC__API_KEY",
        "Set DAIMON_ANTHROPIC__API_KEY in .env to a key from a dedicated Anthropic workspace.",
    ),
)
_DEFAULT_URL = "postgresql+asyncpg://daimon:daimon@localhost:5432/daimon"
_EXISTING_INSTALL_KEYS = (
    *_GENERATED,
    "DAIMON_ANTHROPIC__API_KEY",
    "DAIMON_DISCORD__BOT_TOKEN",
    "DAIMON_SLACK__APP_TOKEN",
    "DAIMON_TEAMS__CLIENT_SECRET",
)
_READY_STEP = (
    "Run `docker compose up --build -d postgres init`. For a CLI first reply, run "
    "`docker compose run --rm --no-deps --entrypoint daimon init sessions create --json`, "
    "then `docker compose run --rm --no-deps --entrypoint daimon init run --session ID "
    '"Hello"` using its session_id.'
)
_GITHUB_OPTIONAL: dict[str, str | bool] = {
    "id": "github_app",
    "command": "daimon github register-app --org <org> --origin <url> --json",
    "why": "let agents read and open PRs on your repos",
    "status": "planned",
    "available": False,
}
_DISCORD_VERIFY_COMMAND = "daimon setup verify discord --guild-id GUILD_ID"


def values_from_env(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)$", line)
        if match:
            raw = match.group(2).strip()
            quote: str | None = None
            for index, char in enumerate(raw):
                if char in "\"'":
                    if quote == char:
                        quote = None
                    elif quote is None:
                        quote = char
                elif char == "#" and quote is None and (index == 0 or raw[index - 1].isspace()):
                    raw = raw[:index].rstrip()
                    break
            values[match.group(1)] = raw.strip("\"'")
    return values


def _set_value(content: str, name: str, value: str) -> str:
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if re.match(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=", line):
            lines[index] = f"{name}={value}\n"
            return "".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    return content + f"{name}={value}\n"


def read_env(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("Environment file is a symlink; choose a regular file with --env-file.")
    if not path.exists():
        return ""
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Environment path must be a regular file.")
    return path.read_text()


def _write_env(path: Path, content: str) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("Environment path changed to a non-regular file.")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def run_setup(env_file: Path) -> dict[str, int | list[str] | list[dict[str, str | bool]] | str]:
    """Prepare missing secrets; return a stable, secret-free JSON payload."""
    content = read_env(env_file)
    values = values_from_env(content)
    # A configured pre-workspace install used the implicit `local` tenant,
    # even if it kept only one secret or kept secrets in the process environment.
    # An untouched .env.example and a new install with only its Anthropic key
    # supplied through the environment still need distinct identities.
    legacy_install = (
        any(values.get(name) for name in _EXISTING_INSTALL_KEYS)
        or values.get("DAIMON_DATABASE__URL") not in (None, "", _DEFAULT_URL)
        or any(
            os.environ.get(name)
            for name in (
                *_GENERATED,
                "DAIMON_DISCORD__BOT_TOKEN",
                "DAIMON_SLACK__APP_TOKEN",
                "DAIMON_TEAMS__CLIENT_SECRET",
            )
        )
        or os.environ.get("DAIMON_DATABASE__URL") not in (None, "", _DEFAULT_URL)
    )
    completed: list[str] = []
    workspace_name = "DAIMON_CLI__WORKSPACE_ID"
    if not values.get(workspace_name):
        workspace_id = "local" if legacy_install else f"install-{secrets.token_hex(16)}"
        content = _set_value(content, workspace_name, workspace_id)
        values[workspace_name] = workspace_id
    completed.append(workspace_name)
    for name in _GENERATED:
        if values.get(name):
            completed.append(name)
            continue
        value = (
            base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            if name == "DAIMON_CRYPTO__KEYS"
            else secrets.token_urlsafe(48)
        )
        content = _set_value(content, name, value)
        values[name] = value
        completed.append(name)
    if not values.get("DAIMON_DATABASE__URL") or values.get("DAIMON_DATABASE__URL") == _DEFAULT_URL:
        content = _set_value(
            content,
            "DAIMON_DATABASE__URL",
            f"postgresql+asyncpg://daimon:{values['POSTGRES_PASSWORD']}@localhost:5432/daimon",
        )
        completed.append("DAIMON_DATABASE__URL")
    if not env_file.exists() or content != read_env(env_file):
        _write_env(env_file, content)
    missing = [
        name for name, _ in _HUMAN_STEPS if not values.get(name) and not os.environ.get(name)
    ]
    required_missing = set(missing)
    next_step = next((step for name, step in _HUMAN_STEPS if name in required_missing), _READY_STEP)
    next_optional = [_GITHUB_OPTIONAL.copy()] if not required_missing else []
    has_discord_token = bool(
        values.get("DAIMON_DISCORD__BOT_TOKEN") or os.environ.get("DAIMON_DISCORD__BOT_TOKEN")
    )
    optional_actions: list[dict[str, str | bool]] = [
        {
            "id": "discord",
            "status": "ready_to_verify" if has_discord_token else "needs_token",
            "command": _DISCORD_VERIFY_COMMAND,
            "why": "connect a Discord server after the local CLI first reply",
            "next_step": (
                f"Run `{_DISCORD_VERIFY_COMMAND}` for your test server."
                if has_discord_token
                else "Create a Discord app and bot, set DAIMON_DISCORD__BOT_TOKEN in .env, "
                f"then run `{_DISCORD_VERIFY_COMMAND}`."
            ),
        }
    ]
    return {
        "schema_version": 1,
        "status": "needs_input" if required_missing else "ready",
        "completed": completed,
        "missing": missing,
        "next_step": next_step,
        "next_optional": next_optional,
        "optional_actions": optional_actions,
    }

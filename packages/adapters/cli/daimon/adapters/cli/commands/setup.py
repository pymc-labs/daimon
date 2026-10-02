"""Noninteractive local setup and read-only Discord preflight."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer
from cryptography.fernet import Fernet

setup_app = typer.Typer(
    help="Prepare local secrets and verify external setup.", invoke_without_command=True
)

_GENERATED = (
    "POSTGRES_PASSWORD",
    "DAIMON_MCP__JWT_SECRET",
    "DAIMON_CRYPTO__KEYS",
)
_HUMAN_STEPS = (
    (
        "DAIMON_ANTHROPIC__API_KEY",
        "Set DAIMON_ANTHROPIC__API_KEY in .env to a key from a dedicated Anthropic workspace.",
    ),
    (
        "DAIMON_DISCORD__BOT_TOKEN",
        "Create a Discord application and bot, then set DAIMON_DISCORD__BOT_TOKEN in .env.",
    ),
    ("DAIMON_MCP__PUBLIC_URL", "Set DAIMON_MCP__PUBLIC_URL in .env to the reachable MCP endpoint."),
)
_DISCORD_API = "https://discord.com/api/v10"
_MESSAGE_CONTENT_FLAGS = (1 << 18) | (1 << 19)
_PERMISSIONS = {
    "send_messages": 1 << 11,
    "embed_links": 1 << 14,
    "read_message_history": 1 << 16,
    "manage_threads": 1 << 34,
    "create_public_threads": 1 << 35,
    "send_messages_in_threads": 1 << 38,
}
_ADMINISTRATOR = 1 << 3
_DEFAULT_URL = "postgresql+asyncpg://daimon:daimon@localhost:5432/daimon"


def _emit(payload: dict[str, Any]) -> None:
    # Only names, instructions and public IDs belong in this object.
    typer.echo(json.dumps(payload, sort_keys=True))


def _values(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("\"'")
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


def _read_env(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("Environment file is a symlink; choose a regular file with --env-file.")
    if not path.exists():
        return ""
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("Environment path must be a regular file.")
    return path.read_text()


def _write_env(path: Path, content: str) -> None:
    # Exclusive temp creation and replace avoid partial secret writes. The
    # destination is checked again immediately before replacement.
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


@setup_app.callback()
def setup(
    ctx: typer.Context,
    env_file: Annotated[
        Path, typer.Option("--env-file", help="Local dotenv file to prepare.")
    ] = Path(".env"),
) -> None:
    """Generate missing local secrets and report exact remaining human steps."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        content = _read_env(env_file)
        values = _values(content)
        completed: list[str] = []
        for name in _GENERATED:
            if values.get(name):
                completed.append(name)
                continue
            value = (
                Fernet.generate_key().decode()
                if name == "DAIMON_CRYPTO__KEYS"
                else secrets.token_urlsafe(48)
            )
            content = _set_value(content, name, value)
            values[name] = value
            completed.append(name)
        if (
            not values.get("DAIMON_DATABASE__URL")
            or values.get("DAIMON_DATABASE__URL") == _DEFAULT_URL
        ):
            content = _set_value(
                content,
                "DAIMON_DATABASE__URL",
                f"postgresql+asyncpg://daimon:{values['POSTGRES_PASSWORD']}@localhost:5432/daimon",
            )
            completed.append("DAIMON_DATABASE__URL")
        if not env_file.exists() or content != _read_env(env_file):
            _write_env(env_file, content)
        missing = [
            name for name, _ in _HUMAN_STEPS if not values.get(name) and not os.environ.get(name)
        ]
        next_step = next((step for name, step in _HUMAN_STEPS if name in missing), None)
        if next_step is None:
            next_step = (
                "Run `daimon setup verify discord --guild-id GUILD_ID` for your test server."
            )
        _emit({"completed": completed, "missing": missing, "next_step": next_step})
    except (OSError, UnicodeError, ValueError):
        _emit(
            {
                "completed": [],
                "missing": [],
                "next_step": "Choose a readable regular environment file with --env-file.",
            }
        )
        raise typer.Exit(1) from None


verify_app = typer.Typer(help="Verify external integrations.")
setup_app.add_typer(verify_app, name="verify")


def _get(client: httpx.Client, path: str) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    try:
        response = client.get(path)
    except httpx.RequestError:
        return None, "Discord API is unreachable; check network access and retry."
    if response.status_code == 401:
        return None, "Discord rejected the bot token (HTTP 401); replace DAIMON_DISCORD__BOT_TOKEN."
    if response.status_code == 403:
        return (
            None,
            "Discord denied access (HTTP 403); check the bot's guild membership and permissions.",
        )
    if response.status_code == 404:
        return (
            None,
            "Discord returned HTTP 404; check the guild ID and invite the bot to that server.",
        )
    if response.status_code != 200:
        return (
            None,
            f"Discord API returned HTTP {response.status_code}; retry.",
        )
    try:
        data = response.json()
    except ValueError:
        return None, "Discord API returned invalid JSON; retry."
    if not isinstance(data, (dict, list)):
        return None, "Discord API returned an unexpected response; retry."
    return cast(dict[str, Any] | list[Any], data), None


@verify_app.command("discord")
def verify_discord(
    guild_id: Annotated[
        str | None, typer.Option("--guild-id", help="Target Discord server ID.")
    ] = None,
    env_file: Annotated[
        Path, typer.Option("--env-file", help="Dotenv file containing the bot token.")
    ] = Path(".env"),
) -> None:
    """Check bot token, message content intent, guild membership and permissions."""
    completed: list[str] = []
    missing: list[str] = []
    failures: list[str] = []
    try:
        values = _values(_read_env(env_file))
    except (OSError, UnicodeError, ValueError):
        values = {}
        failures.append(
            "Cannot read the environment file; provide a regular readable file with --env-file."
        )
    token = os.environ.get("DAIMON_DISCORD__BOT_TOKEN") or values.get("DAIMON_DISCORD__BOT_TOKEN")
    if not token:
        missing.append("DAIMON_DISCORD__BOT_TOKEN")
        failures.append("Set DAIMON_DISCORD__BOT_TOKEN in .env to the Discord bot token.")
    if not guild_id:
        missing.append("guild_id")
        failures.append("Pass --guild-id GUILD_ID for the server where the bot should run.")
    elif not guild_id.isdecimal():
        failures.append("--guild-id must be a numeric Discord server ID.")
    if token:
        with httpx.Client(
            base_url=_DISCORD_API, headers={"Authorization": f"Bot {token}"}, timeout=10.0
        ) as client:
            user, error = _get(client, "/users/@me")
            if error:
                failures.append(f"Token check: {error}")
            elif (
                not isinstance(user, dict)
                or not user.get("bot")
                or not str(user.get("id", "")).isdecimal()
            ):
                failures.append("Token check: token did not identify a Discord bot user.")
            else:
                completed.append("token")
                application, error = _get(client, "/oauth2/applications/@me")
                if error:
                    failures.append(f"Intent check: {error}")
                elif not isinstance(application, dict) or not isinstance(
                    application.get("flags"), int
                ):
                    failures.append(
                        "Intent check: application flags unavailable; check Message Content Intent "
                        "in the Developer Portal."
                    )
                elif application["flags"] & _MESSAGE_CONTENT_FLAGS:
                    completed.append("message_content_intent")
                else:
                    failures.append(
                        "Message Content Intent is disabled or unapproved; enable it under Bot "
                        "in the Discord Developer Portal."
                    )
                if guild_id and guild_id.isdecimal():
                    member, error = _get(client, f"/guilds/{guild_id}/members/{user['id']}")
                    if error:
                        failures.append(f"Guild membership: {error}")
                    elif not isinstance(member, dict):
                        failures.append(
                            "Guild membership: Discord returned an unexpected member response."
                        )
                    else:
                        completed.append("guild_membership")
                        roles, error = _get(client, f"/guilds/{guild_id}/roles")
                        if error:
                            failures.append(f"Permissions: {error}")
                        elif not isinstance(roles, list):
                            failures.append(
                                "Permissions: Discord returned an unexpected role response."
                            )
                        else:
                            member_roles = member.get("roles")
                            if not isinstance(member_roles, list) or not all(
                                isinstance(item, str) for item in cast(list[Any], member_roles)
                            ):
                                failures.append(
                                    "Permissions: Discord returned invalid member roles."
                                )
                                member_roles = []
                            role_ids: set[str] = {guild_id}
                            role_ids.update(cast(list[str], member_roles))
                            permissions = 0
                            for role in roles:
                                if isinstance(role, dict):
                                    role_data = cast(dict[str, Any], role)
                                    if role_data.get("id") not in role_ids:
                                        continue
                                    try:
                                        permissions |= int(role_data["permissions"])
                                    except (KeyError, TypeError, ValueError):
                                        failures.append(
                                            "Permissions: invalid role permission value."
                                        )
                            if not any(item.startswith("Permissions:") for item in failures):
                                if permissions & _ADMINISTRATOR:
                                    completed.append("guild_permissions")
                                else:
                                    absent = [
                                        name
                                        for name, bit in _PERMISSIONS.items()
                                        if not permissions & bit
                                    ]
                                    if absent:
                                        failures.append(
                                            "Missing guild permissions: "
                                            + ", ".join(sorted(absent))
                                            + ". Grant them to the bot's roles in Discord."
                                        )
                                    else:
                                        completed.append("guild_permissions")
    next_step = failures[0] if failures else "Discord verification passed."
    _emit(
        {"completed": completed, "missing": missing, "failures": failures, "next_step": next_step}
    )
    if failures:
        raise typer.Exit(1)

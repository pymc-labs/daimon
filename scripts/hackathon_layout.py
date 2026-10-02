"""Create or remove one private Discord channel and Daimon agent per event team.

Run where the ``daimon`` CLI has staging or event deployment credentials. Secrets
are read only from environment variables. Keep the state file for resuming and
teardown. Example::

    python scripts/hackathon_layout.py --guild-id 1435062989119295640 \
      --teams teams.csv --state layout-state.json --run-id rehearsal-3 \
      --budget-usd 25 --threads-per-team 3 --bot-token-env DISCORD_QA_BOT_TOKEN

For the event guild, add ``--event --allow-guild-id ID``. This explicit
allow-list prevents a staging rehearsal from writing to another server.
"""

from __future__ import annotations

# pyright: basic
import argparse
import csv
import json
import os
import re
import subprocess
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

QA_GUILD = "1435062989119295640"
QA_BOT_ID = "1533049261032341668"
API = "https://discord.com/api/v10"
VIEW = 1 << 10
SEND = 1 << 11
READ_HISTORY = 1 << 16
SEND_IN_THREADS = 1 << 38
CREATE_PUBLIC_THREADS = 1 << 35
MANAGE_MESSAGES = 1 << 13
MANAGE_ROLES = 1 << 28
MANAGE_CHANNELS = 1 << 4


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not result:
        raise ValueError(f"team name has no usable characters: {value!r}")
    return result[:45]


def read_teams(path: Path) -> list[tuple[str, list[str]]]:
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows or "team name" not in rows[0]:
        raise ValueError("CSV needs a 'team name' column and at least one team")
    teams = []
    seen = set()
    for row in rows:
        name = (row["team name"] or "").strip()
        key = slug(name)
        if key in seen:
            raise ValueError(f"duplicate team slug: {key}")
        seen.add(key)
        members = [v.strip() for v in (row.get("member Discord ids") or "").split(";") if v.strip()]
        if any(not member.isdigit() for member in members):
            raise ValueError(f"non-numeric Discord member id for {name}")
        teams.append((name, members))
    return teams


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


class Discord:
    def __init__(self, token: str) -> None:
        self.http = httpx.Client(
            base_url=API, headers={"Authorization": f"Bot {token}"}, timeout=30
        )
        self.created: deque[float] = deque()

    def request(self, method: str, path: str, **kwargs) -> Any:  # noqa: ANN401
        for _ in range(12):
            response = self.http.request(method, path, **kwargs)
            if response.status_code == 429:
                wait = float(response.json().get("retry_after", 5))
                time.sleep(wait + 0.2)
                continue
            if response.status_code == 404 and method == "DELETE":
                return {}
            response.raise_for_status()
            return response.json() if response.content else {}
        raise RuntimeError(f"Discord rate limit persisted on {method} {path}")

    def pace_thread(self) -> None:
        # Leave margin under the observed guild buckets: 10/10s and 50/300s.
        while True:
            now = time.monotonic()
            while self.created and now - self.created[0] >= 300:
                self.created.popleft()
            recent = [t for t in self.created if now - t < 10]
            if len(recent) < 8 and len(self.created) < 45:
                self.created.append(now)
                return
            wait = recent[0] + 10 - now if len(recent) >= 8 else self.created[0] + 300 - now
            time.sleep(max(wait, 0.1) + 0.2)


def cli(*args: str) -> str:
    result = subprocess.run(["daimon", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def parse_agent(output: str) -> str:
    marker = ": isolated, agent "
    if marker not in output:
        raise RuntimeError(f"could not read isolated agent from CLI output: {output}")
    return output.rsplit(marker, 1)[1].strip()


def resolve_role(api: Discord, guild: str, name: str) -> str | None:
    roles = api.request("GET", f"/guilds/{guild}/roles")
    matches = [str(role["id"]) for role in roles if role["name"] == name]
    if len(matches) > 1:
        raise RuntimeError(f"ambiguous role {name}")
    return matches[0] if matches else None


def require_setup_permissions(api: Discord, guild: str, bot_id: str) -> None:
    """Fail before any write if the selected bot cannot build the layout."""
    member = api.request("GET", f"/guilds/{guild}/members/{bot_id}")
    roles = api.request("GET", f"/guilds/{guild}/roles")
    held = set(member["roles"]) | {guild}
    permissions = 0
    for role in roles:
        if role["id"] in held:
            permissions |= int(role["permissions"])
    missing = [
        label
        for label, bit in (("Manage Channels", MANAGE_CHANNELS), ("Manage Roles", MANAGE_ROLES))
        if not permissions & bit
    ]
    if missing:
        raise RuntimeError(f"bot lacks {', '.join(missing)} in guild {guild}")


def resolve_channel(api: Discord, guild: str, name: str) -> str | None:
    channels = api.request("GET", f"/guilds/{guild}/channels")
    matches = [str(channel["id"]) for channel in channels if channel["name"] == name]
    if len(matches) > 1:
        raise RuntimeError(f"ambiguous channel {name}")
    return matches[0] if matches else None


def setup_team(
    api: Discord, args: argparse.Namespace, state: dict, name: str, members: list[str]
) -> None:
    key = slug(name)
    entry = state["teams"].setdefault(key, {"name": name, "members": members, "threads": []})
    prefix = f"{slug(args.run_id)}-{key}"
    role_name, channel_name = f"team-{prefix}"[:100], f"team-{prefix}"[:100]
    guild = args.guild_id
    if not entry.get("role_id"):
        entry["role_id"] = resolve_role(api, guild, role_name) or str(
            api.request(
                "POST", f"/guilds/{guild}/roles", json={"name": role_name, "mentionable": True}
            )["id"]
        )
        save(args.state, state)
    role_id = entry["role_id"]
    if not entry.get("channel_id"):
        bot_id = str(api.request("GET", "/users/@me")["id"])
        overwrites = [
            {"id": guild, "type": 0, "deny": str(VIEW), "allow": "0"},
            {
                "id": role_id,
                "type": 0,
                "allow": str(VIEW | SEND | READ_HISTORY | SEND_IN_THREADS | CREATE_PUBLIC_THREADS),
                "deny": "0",
            },
            {
                "id": bot_id,
                "type": 1,
                "allow": str(
                    VIEW
                    | SEND
                    | READ_HISTORY
                    | SEND_IN_THREADS
                    | CREATE_PUBLIC_THREADS
                    | MANAGE_MESSAGES
                ),
                "deny": "0",
            },
        ]
        entry["channel_id"] = resolve_channel(api, guild, channel_name) or str(
            api.request(
                "POST",
                f"/guilds/{guild}/channels",
                json={"name": channel_name, "type": 0, "permission_overwrites": overwrites},
            )["id"]
        )
        save(args.state, state)
    channel_id = entry["channel_id"]
    for member in members:
        api.request("PUT", f"/guilds/{guild}/members/{member}/roles/{role_id}")
    if not entry.get("agent_name"):
        entry["agent_name"] = parse_agent(
            cli(
                "channels",
                "isolation",
                "set",
                "discord",
                guild,
                channel_id,
                "--fork-from",
                args.fork_from,
            )
        )
        save(args.state, state)
    if not entry.get("budget_set"):
        cli(
            "channels",
            "budget",
            "set",
            "discord",
            guild,
            channel_id,
            str(args.budget_usd),
            "--window",
            "total",
        )
        entry["budget_set"] = True
        save(args.state, state)
    if not entry.get("admins_set"):
        cli("channels", "admins", "set", "discord", guild, channel_id, "--role", role_id)
        entry["admins_set"] = True
        save(args.state, state)
    # Discord's active-thread listing lets a rerun recover a create that
    # succeeded immediately before the state file was written.
    active = api.request("GET", f"/guilds/{guild}/threads/active")
    by_name = {
        thread["name"]: str(thread["id"])
        for thread in active.get("threads", [])
        if str(thread.get("parent_id")) == channel_id
    }
    for index in range(1, args.threads_per_team + 1):
        thread_name = f"Working thread {index}"
        if len(entry["threads"]) >= index:
            continue
        thread_id = by_name.get(thread_name)
        if thread_id is None:
            api.pace_thread()
            thread_id = str(
                api.request(
                    "POST",
                    f"/channels/{channel_id}/threads",
                    json={"name": thread_name, "type": 11, "auto_archive_duration": 10080},
                )["id"]
            )
        entry["threads"].append(thread_id)
        save(args.state, state)
    if not entry.get("pinned_message_id"):
        links = " ".join(
            f"[Thread {i}](https://discord.com/channels/{guild}/{tid})"
            for i, tid in enumerate(entry["threads"], 1)
        )
        message = api.request(
            "POST",
            f"/channels/{channel_id}/messages",
            json={"content": f"Start here: mention @Daimon in one of these threads. {links}"},
        )
        entry["pinned_message_id"] = str(message["id"])
        save(args.state, state)
    api.request("PUT", f"/channels/{channel_id}/pins/{entry['pinned_message_id']}")
    print(
        f"{name}: role={role_id} channel={channel_id} "
        f"agent={entry['agent_name']} threads={len(entry['threads'])}",
        flush=True,
    )


def teardown_team(api: Discord, args: argparse.Namespace, state: dict, key: str) -> None:
    entry = state["teams"][key]
    guild = args.guild_id
    channel = entry.get("channel_id")
    if channel:
        cli("channels", "budget", "clear", "discord", guild, channel)
        cli("channels", "admins", "clear", "discord", guild, channel)
        cli("channels", "isolation", "lift", "discord", guild, channel)
        if entry.get("agent_name"):
            cli("agents", "--guild", guild, "archive", entry["agent_name"], "--yes")
        api.request("DELETE", f"/channels/{channel}")
    if entry.get("role_id"):
        api.request("DELETE", f"/guilds/{guild}/roles/{entry['role_id']}")
    del state["teams"][key]
    save(args.state, state)
    print(f"removed {key}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--allow-guild-id", action="append", default=[])
    parser.add_argument("--event", action="store_true")
    parser.add_argument("--teams", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--budget-usd", type=Decimal, default=Decimal("25"))
    parser.add_argument("--threads-per-team", type=int, default=3)
    parser.add_argument("--fork-from", default="daimon")
    parser.add_argument("--bot-token-env", default="DISCORD_QA_BOT_TOKEN")
    parser.add_argument("--teardown", action="store_true")
    args = parser.parse_args()
    if args.guild_id != QA_GUILD and not (args.event and args.guild_id in args.allow_guild_id):
        parser.error("non-QA guild requires --event and matching --allow-guild-id")
    if args.threads_per_team < 1 or args.threads_per_team > 10 or args.budget_usd < 0:
        parser.error("threads-per-team must be 1..10 and budget nonnegative")
    token = os.environ.get(args.bot_token_env)
    if not token:
        parser.error(f"{args.bot_token_env} is unset")
    teams = read_teams(args.teams)
    state = (
        json.loads(args.state.read_text())
        if args.state.exists()
        else {"guild_id": args.guild_id, "run_id": args.run_id, "teams": {}}
    )
    if state["guild_id"] != args.guild_id or state["run_id"] != args.run_id:
        parser.error("state file guild/run id does not match")
    api = Discord(token)
    bot = api.request("GET", "/users/@me")
    if args.guild_id == QA_GUILD and str(bot["id"]) != QA_BOT_ID:
        parser.error("staging layout requires the QA bot token")
    if not args.teardown:
        require_setup_permissions(api, args.guild_id, str(bot["id"]))
    if args.teardown:
        for key in list(state["teams"]):
            teardown_team(api, args, state, key)
    else:
        for name, members in teams:
            setup_team(api, args, state, name, members)


if __name__ == "__main__":
    main()

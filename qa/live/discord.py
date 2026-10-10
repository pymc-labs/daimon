"""Reuse the installed stdlib qa.py driver, adding ownership and evidence bounds."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Protocol, cast

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from qa.live.config import Config, Target
from qa.live.schema import Assertion, Step
from qa.live.types import Message, Pending, Turn, Usage, obj, objects, text_of, utcnow


class Driver(Protocol):
    def set_role(self, role: str) -> None: ...

    TERMINAL: frozenset[str]

    def call(self, method: str, path: str, *, body: Message | None = None) -> JsonValue: ...
    def messages(self, channel_id: str, *, after: str | None, limit: int = 50) -> list[Message]: ...
    def turn_messages(self, channel_id: str, *, after: str, daimon_id: str) -> list[Message]: ...
    def exists(self, channel: str) -> bool: ...
    def classify(self, message: Message) -> str: ...
    def cmd_say(self, args: argparse.Namespace) -> None: ...
    def cmd_warm(self, args: argparse.Namespace) -> None: ...
    def cmd_access(self, args: argparse.Namespace) -> None: ...
    def cmd_selftest(self, args: argparse.Namespace) -> None: ...
    def effective_permissions(
        self, *, guild: Message, overwrites: list[Message], user_id: str, api_guild_id: str
    ) -> int: ...


def load_driver(path: Path) -> Driver:
    spec = importlib.util.spec_from_file_location("daimon_qa_legacy_driver", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load the qa.py driver")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for public, legacy in (
        ("call", "_call"),
        ("messages", "_messages"),
        ("exists", "_exists"),
        ("effective_permissions", "_effective_permissions"),
    ):
        setattr(module, public, getattr(module, legacy))

    def set_role(role: str) -> None:
        module.__dict__["_role"] = role

    module.__dict__["set_role"] = set_role
    return cast(Driver, cast(object, module))


def fingerprint(message: Message) -> str:
    return json.dumps(
        {
            k: message.get(k)
            for k in (
                "id",
                "content",
                "embeds",
                "attachments",
                "edited_timestamp",
            )
        },
        sort_keys=True,
    )


class DiscordBackend:
    def __init__(self, config: Config, env: str, *, driver: Driver | None = None) -> None:
        self.config = config
        self.env = env
        self.target: Target = config.target(env)
        self.driver = driver or load_driver(Path(config.driver_path))
        self.owned: set[str] = set()
        self.threads: set[str] = set()
        self.baselines: dict[str, set[str]] = {}
        self.parent = ""

    def context(self) -> dict[str, str]:
        return {
            "guild_id": self.target.guild_id,
            **{f"env.{key}": value for key, value in self.target.context.items()},
        }

    def preflight(self, roles: set[str]) -> None:
        self.driver.set_role("user")
        self.driver.cmd_selftest(argparse.Namespace())
        category = obj(self.driver.call("GET", f"/channels/{self.target.category_id}"))
        if category.get("guild_id") != self.target.guild_id or category.get("type") != 4:
            raise ValueError("QA parent must be a category in the configured guild")
        guild = obj(self.driver.call("GET", f"/guilds/{self.target.guild_id}"))
        for role in roles | {"user"}:
            self.driver.set_role(role)
            me = obj(self.driver.call("GET", "/users/@me"))
            qa_id = str(me.get("id", ""))
            if qa_id not in self.target.qa_user_ids:
                raise ValueError("QA bot identity is not configured in the allow-list")
            self.driver.cmd_access(
                argparse.Namespace(
                    parent=self.target.category_id,
                    guild=self.target.guild_id,
                    qa=qa_id,
                    daimon=self.target.daimon_id,
                )
            )
            for user_id, needed in (
                (qa_id, (1 << 4) | (1 << 6) | (1 << 15)),
                (self.target.daimon_id, (1 << 14) | (1 << 18) | (1 << 34)),
            ):
                effective = self.driver.effective_permissions(
                    guild=guild,
                    overwrites=objects(category.get("permission_overwrites")),
                    user_id=user_id,
                    api_guild_id=self.target.guild_id,
                )
                if effective & needed != needed:
                    raise ValueError(
                        "QA category lacks channel/thread/embed/attachment permissions"
                    )
        self.driver.set_role("user")
        if self.env == "staging":
            self.driver.cmd_warm(argparse.Namespace(url=self.target.warm_url, timeout=90))

    def create_channel(self, name: str) -> str:
        self.driver.set_role("user")
        channel = obj(
            self.driver.call(
                "POST",
                f"/guilds/{self.target.guild_id}/channels",
                body={
                    "name": name[:100],
                    "type": 0,
                    "parent_id": self.target.category_id,
                },
            )
        )
        channel_id = str(channel["id"])
        self.owned.add(channel_id)
        if not self.parent:
            self.parent = channel_id
        return channel_id

    def _owned(self, channel: str) -> None:
        if channel not in self.owned | self.threads:
            raise ValueError("refusing to write outside a channel created by this run")

    def delete_channel(self, channel: str) -> None:
        self._owned(channel)
        self.driver.set_role("user")
        self.driver.call("DELETE", f"/channels/{channel}")
        self.owned.discard(channel)

    def send(
        self,
        channel: str,
        step: Step,
        *,
        mention: bool,
        reply_message_id: str | None = None,
    ) -> str:
        self._owned(channel)
        self.driver.set_role(step.role)
        baseline = self.driver.messages(channel, after=None, limit=100)
        content = (f"<@{self.target.daimon_id}> " if mention else "") + (step.text or "")
        payload: Message = {"content": content}
        if reply_message_id:
            payload["message_reference"] = {"message_id": reply_message_id, "channel_id": channel}
        if step.file and reply_message_id:
            raise Pending("file with reply reference is not yet supported by qa.py")
        if step.file:
            # Reuse qa.py's multipart upload; only the emitted message ID is captured.
            import contextlib
            import io

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.driver.cmd_say(
                    argparse.Namespace(channel=channel, text=content, file=step.file)
                )
            message_id = output.getvalue().strip()
        else:
            result = obj(
                self.driver.call(
                    "POST",
                    f"/channels/{channel}/messages",
                    body=payload,
                )
            )
            message_id = str(result["id"])
        if not message_id.isdigit():
            raise ValueError("driver did not return a Discord message id")
        self.baselines[message_id] = {fingerprint(m) for m in baseline}
        self.driver.set_role("user")
        return message_id

    def collect(self, turn: Turn, timeout: float) -> None:
        self._owned(turn.channel_id)
        self.driver.set_role("user")
        turn.guild_id = self.target.guild_id
        deadline = time.monotonic() + timeout
        stable_since: float | None = None
        previous = ""
        while time.monotonic() < deadline:
            candidates = self.driver.turn_messages(
                turn.channel_id,
                after=turn.trigger_id,
                daimon_id=self.target.daimon_id,
            )
            raw = self.driver.messages(turn.channel_id, after=None, limit=100)
            if self.driver.exists(turn.trigger_id):
                raw.extend(self.driver.messages(turn.trigger_id, after=None, limit=100))
            identity_authors = {
                str(obj(m.get("author"))["id"])
                for m in raw
                if m.get("webhook_id")
                and obj(m.get("author")).get("id")
                and str(m.get("application_id")) == self.target.daimon_id
            }
            for author_id in identity_authors:
                candidates.extend(
                    self.driver.turn_messages(
                        turn.channel_id,
                        after=turn.trigger_id,
                        daimon_id=author_id,
                    )
                )
            candidates = list({str(m.get("id")): m for m in candidates}.values())
            messages = [
                m
                for m in candidates
                if fingerprint(m) not in self.baselines.get(turn.trigger_id, set())
            ]
            elapsed = (utcnow() - turn.started_at).total_seconds()
            # Queue reactions on the trigger count as visible output even without an answer.
            trigger = obj(
                self.driver.call(
                    "GET",
                    f"/channels/{turn.channel_id}/messages/{turn.trigger_id}",
                )
            )
            visible_reaction = False
            turn.trigger_reactions = []
            for reaction in objects(trigger.get("reactions")):
                emoji = obj(reaction.get("emoji"))
                name = str(emoji.get("name", ""))
                if emoji.get("id"):
                    name += ":" + str(emoji["id"])
                users = objects(
                    self.driver.call(
                        "GET",
                        f"/channels/{turn.channel_id}/messages/{turn.trigger_id}/reactions/"
                        f"{urllib.parse.quote(name, safe='')}?limit=100",
                    )
                )
                if any(u.get("id") == self.target.daimon_id for u in users):
                    visible_reaction = True
                    turn.trigger_reactions.append(reaction)
            if (messages or visible_reaction) and turn.first_visible_s is None:
                turn.first_visible_s = elapsed
            turn.messages = messages
            turn.parent_messages = [m for m in messages if str(m.get("channel_id")) in self.owned]
            thread_ids = {
                str(m["channel_id"])
                for m in messages
                if m.get("channel_id") and str(m.get("channel_id")) not in self.owned
            }
            if thread_ids:
                if len(thread_ids) != 1:
                    raise ValueError("turn unexpectedly answered in multiple threads")
                turn.thread_id = next(iter(thread_ids))
                self.threads.add(turn.thread_id)
            turn.verdicts = [self.classify(m) for m in messages]
            if "working" in turn.verdicts and turn.progress_seen_s is None:
                turn.progress_seen_s = elapsed
            terminal = bool(messages) and all(v in self.driver.TERMINAL for v in turn.verdicts)
            current = json.dumps(messages, sort_keys=True)
            if terminal:
                if turn.done_s is None:
                    turn.done_s = elapsed
                if current != previous or stable_since is None:
                    stable_since = time.monotonic()
                if time.monotonic() - stable_since >= self.config.settle_s:
                    turn.ended_at = utcnow()
                    return
            else:
                turn.done_s = None
                stable_since = None
            previous = current
            time.sleep(min(self.config.poll_interval_s, max(0, deadline - time.monotonic())))
        turn.ended_at = utcnow()
        raise Pending("watch timed out; collected last messages, no terminal proof")

    def react(self, channel: str, message: str, emoji: str) -> None:
        self._owned(channel)
        self.driver.set_role("user")
        self.driver.call(
            "PUT",
            f"/channels/{channel}/messages/{message}/reactions/"
            f"{urllib.parse.quote(emoji, safe='')}/@me",
        )

    def admin(self, step: Step, channel: str) -> None:
        self._owned(channel)
        if self.env != "staging":
            raise ValueError("admin and restart actions are forbidden in production")
        tool = "restart_workers" if step.do == "restart_workers" else (step.tool or "")
        command = self.config.admin_hooks.get(tool)
        if not command:
            raise Pending(f"staging admin hook is not configured: {tool}")
        context = {
            "guild_id": self.target.guild_id,
            "channel_id": channel,
            "role": step.role,
            "args": step.args,
        }
        result = subprocess.run(
            command,
            input=json.dumps(context),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("configured staging admin hook failed")

    def logs(self, assertion: Assertion, turn: Turn) -> list[Message]:
        if not turn.thread_id or not turn.ended_at:
            raise Pending("logs require a thread id and bounded turn window")
        window = " AND ".join(
            [
                f"timestamp>={json.dumps(turn.started_at.isoformat())}",
                f"timestamp<={json.dumps(turn.ended_at.isoformat())}",
            ]
        )
        thread_scope = f"jsonPayload.thread_id={json.dumps(turn.thread_id)}"
        anchors = self._read_logs(f"{window} AND {thread_scope}")
        # Core preparation events carry rid but no thread_id. Discover that rid only
        # through this turn's thread-scoped logs, never through an unscoped event search.
        rids = {
            str(obj(row.get("jsonPayload"))["rid"])
            for row in anchors
            if obj(row.get("jsonPayload")).get("rid")
        }
        scope = " OR ".join(
            [thread_scope, *[f"jsonPayload.rid={json.dumps(rid)}" for rid in sorted(rids)]]
        )
        if assertion.kind == "log_absent" and not rids:
            raise Pending("no request-correlated logs; event absence is unproven")
        query = f"{window} AND ({scope}) AND jsonPayload.event={json.dumps(assertion.event)}"
        rows = self._read_logs(query)
        return [
            row
            for row in rows
            if all(
                obj(row.get("jsonPayload")).get(key) == value
                for key, value in assertion.fields.items()
            )
        ]

    def _read_logs(self, query: str) -> list[Message]:
        try:
            result = subprocess.run(
                [
                    "gcloud",
                    "logging",
                    "read",
                    query,
                    f"--project={self.target.project}",
                    "--format=json",
                    "--limit=1000",
                    "--order=asc",
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Pending("Cloud Logging query is unavailable") from exc
        if result.returncode:
            raise Pending("Cloud Logging query failed; absence is unproven")
        try:
            rows = objects(json.loads(result.stdout))
        except ValueError as exc:
            raise Pending("Cloud Logging returned malformed evidence") from exc
        if len(rows) >= 1000:
            raise Pending("Cloud Logging results were truncated")
        return rows

    async def _query(self, sql: str, params: dict[str, object] | None = None) -> JsonValue:
        import re

        if not re.match(r"(?is)^\s*(?:SELECT|WITH)\b", sql):
            raise Pending("db_check permits only SELECT/WITH queries")
        url = os.environ.get(self.target.database_env)
        if not url:
            raise Pending(f"read-only database unavailable: {self.target.database_env}")
        if not url.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise Pending("QA evidence database must be PostgreSQL")
        engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(text("SET LOCAL statement_timeout = '15000ms'"))
                rows = await conn.execute(text(sql), params or {})
                return cast(
                    JsonValue,
                    json.loads(
                        json.dumps(
                            [dict(row) for row in rows.mappings()],
                            default=str,
                        )
                    ),
                )
        finally:
            await engine.dispose()

    def db_check(self, sql: str, turn: Turn | None = None) -> JsonValue:
        if "{headless." in sql:
            raise Pending("headless SQL context is not available")
        params: dict[str, object] = {"guild_id": self.target.guild_id, "channel_id": self.parent}
        if turn:
            params.update(thread_id=turn.thread_id, trigger_message_id=turn.trigger_id)
        if ":tenant_id" in sql:
            tenants = objects(
                asyncio.run(
                    self._query(
                        "SELECT id FROM tenants WHERE platform = 'discord' "
                        "AND external_id = :guild",
                        {"guild": self.target.guild_id},
                    )
                )
            )
            if len(tenants) != 1:
                raise Pending("QA tenant is not uniquely resolved")
            params["tenant_id"] = tenants[0]["id"]
        try:
            result = asyncio.run(self._query(sql, params))
        except Pending:
            raise
        except Exception as exc:
            raise Pending(f"read-only SQL evidence unavailable: {type(exc).__name__}") from exc
        rows = objects(result)
        if len(rows) == 1 and len(rows[0]) == 1:
            return next(iter(rows[0].values()))
        return result

    def usage(self, turn: Turn) -> Usage:
        try:
            rows = objects(
                asyncio.run(
                    self._query(
                        "SELECT input_tokens, output_tokens, cache_read_input_tokens, "
                        "cache_creation_input_tokens, cost_usd, model_ids FROM turn_outcomes "
                        "WHERE thread_id = :thread AND started_at >= :start AND started_at <= :end "
                        "ORDER BY started_at",
                        {
                            "thread": turn.thread_id,
                            "start": turn.started_at,
                            "end": turn.ended_at or utcnow(),
                        },
                    )
                )
            )
            if len(rows) == 1 and rows[0].get("cost_usd") is not None:
                row = rows[0]
                models = row.get("model_ids")
                return Usage(
                    input_tokens=int(str(row["input_tokens"]))
                    if row.get("input_tokens") is not None
                    else None,
                    output_tokens=int(str(row["output_tokens"]))
                    if row.get("output_tokens") is not None
                    else None,
                    cache_read_input_tokens=int(str(row["cache_read_input_tokens"]))
                    if row.get("cache_read_input_tokens") is not None
                    else None,
                    cache_creation_input_tokens=int(str(row["cache_creation_input_tokens"]))
                    if row.get("cache_creation_input_tokens") is not None
                    else None,
                    usd=float(str(row["cost_usd"])),
                    source="turn_outcomes",
                    models=[str(m) for m in models] if isinstance(models, list) else [],
                )
        except Exception:
            pass
        # Older deployments retain tokens and price in the footer. Current ones only carry price.
        import re

        footers = "\n".join(text_of(m) for m in turn.messages)
        cost = re.search(r"\$(\d+(?:\.\d+)?)\s+used", footers)
        tokens = re.search(r"([\d.]+)(k?) in / ([\d.]+)(k?) out", footers)
        old_cost = re.search(r"·\s*\$(\d+(?:\.\d+)?)", footers)
        if cost or old_cost:
            match = cost or old_cost
            assert match is not None
            return Usage(
                input_tokens=int(float(tokens[1]) * (1000 if tokens[2] else 1)) if tokens else None,
                output_tokens=int(float(tokens[3]) * (1000 if tokens[4] else 1))
                if tokens
                else None,
                usd=float(match[1]),
                source="footer_rounded",
            )
        return Usage()

    def classify(self, message: Message) -> str:
        return self.driver.classify(message)

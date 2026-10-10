"""Reuse the installed stdlib qa.py driver, adding ownership and evidence bounds."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from qa.live.config import Config, Target
from qa.live.errors import redact
from qa.live.schema import Assertion, Step
from qa.live.types import Message, Pending, Turn, Usage, WatchTimeout, obj, objects, text_of, utcnow

CHANNEL_MARKER = "daimon-live-qa"


def log_scope(field: str, value: str) -> str:
    structured = f"jsonPayload.{field}={json.dumps(value)}"
    numeric = f" OR jsonPayload.{field}={value}" if value.isdigit() else ""
    # GCE's container logger wraps the event as a JSON string. Query broadly
    # there, then enforce exact parsed IDs/events before accepting evidence.
    wrapped = (
        f"(jsonPayload.message:{json.dumps(field)} AND jsonPayload.message:{json.dumps(value)})"
    )
    return f"({structured}{numeric} OR {wrapped})"


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


def message_created_at(message: Message) -> datetime | None:
    timestamp = message.get("timestamp")
    if isinstance(timestamp, str):
        created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("Discord timestamp lacks a timezone")
        return created
    identity = str(message.get("id", ""))
    if identity.isdigit() and int(identity) >= 1 << 22:
        return datetime.fromtimestamp(((int(identity) >> 22) + 1420070400000) / 1000, UTC)
    return None


def message_visible_at(
    message: Message, trigger_at: datetime, *, reused: bool = False
) -> tuple[datetime, str] | None:
    created = message_created_at(message)
    # Allow the legacy driver's documented 5s cross-worker snowflake skew.
    # A reused card predating this trigger needs an edit from this turn.
    if created and not reused and (created - trigger_at).total_seconds() >= -5:
        source = "message_created" if created >= trigger_at else "message_created_clock_skew"
        return created, source
    edited = message.get("edited_timestamp")
    if isinstance(edited, str):
        timestamp = datetime.fromisoformat(edited.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Discord edit timestamp lacks a timezone")
        if (timestamp - trigger_at).total_seconds() >= -5:
            return timestamp, "card_edited"
    return None


class DiscordBackend:
    def __init__(self, config: Config, env: str, *, driver: Driver | None = None) -> None:
        self.config = config
        self.env = env
        self.target: Target = config.target(env)
        self._driver = driver
        self.owned: set[str] = set()
        self.threads: set[str] = set()
        self.baselines: dict[str, set[str]] = {}
        self.baseline_ids: dict[str, set[str]] = {}
        self.parent = ""
        self.thread_parents: dict[str, str] = {}
        self.fallback_watch_s = config.fallback_watch_s
        self.bindings: dict[str, str] = {}

    @property
    def driver(self) -> Driver:
        if self._driver is None:
            self._driver = load_driver(Path(self.config.driver_path))
        return self._driver

    def context(self) -> dict[str, str]:
        return {
            **self.bindings,
            "guild_id": self.target.guild_id,
            **{f"env.{key}": value for key, value in self.target.context.items()},
        }

    def preflight(self, roles: set[str]) -> None:
        if not os.environ.get(self.target.database_env):
            raise Pending("read-only usage database is required before a live run")
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
        self.sweep_orphans()
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
                    "topic": CHANNEL_MARKER,
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
        for attempt in range(3):
            try:
                self.driver.call("DELETE", f"/channels/{channel}")
                break
            except (Exception, SystemExit):
                if attempt == 2:
                    raise
                time.sleep(self.config.delete_retry_s * (attempt + 1))
        self.owned.discard(channel)

    def sweep_orphans(self) -> None:
        channels = objects(self.driver.call("GET", f"/guilds/{self.target.guild_id}/channels"))
        swept = 0
        for channel in channels:
            identity = str(channel.get("id", ""))
            if (
                channel.get("parent_id") != self.target.category_id
                or not str(channel.get("name", "")).startswith("qa-")
                or channel.get("topic") != CHANNEL_MARKER
                or not identity.isdigit()
            ):
                continue
            created = ((int(identity) >> 22) + 1420070400000) / 1000
            if utcnow().timestamp() - created < self.config.orphan_after_s:
                continue
            if swept >= 20:
                raise Pending("stale QA channel sweep reached its bounded limit")
            self.owned.add(identity)
            self.delete_channel(identity)
            swept += 1

    def verify_model(self, channel: str) -> None:
        from qa.live.model_probe import ModelEvidence

        self._owned(channel)
        parent = channel if channel in self.owned else self.thread_parents.get(channel)
        if not parent:
            raise Pending("thread has no verified QA parent for model readback")
        if not self.target.model_probe or not self.target.qa_agent_name:
            raise Pending("read-only deployment model probe and QA agent name are required")
        request = {
            "guild_id": self.target.guild_id,
            "channel_id": parent,
            "category_id": self.target.category_id,
        }
        for attempt in range(2):
            try:
                response = subprocess.run(
                    self.target.model_probe,
                    input=json.dumps(request),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                if attempt:
                    raise
                time.sleep(2)
                continue
            if not response.returncode:
                break
            if attempt:
                raise RuntimeError(
                    f"read-only deployment model probe exited {response.returncode}; "
                    f"stdout={redact(response.stdout)}; stderr={redact(response.stderr)}"
                )
            time.sleep(2)
        else:
            raise RuntimeError("read-only deployment model probe did not finish")
        evidence = ModelEvidence.model_validate_json(response.stdout)
        if (
            evidence.guild_id != self.target.guild_id
            or evidence.channel_id != parent
            or evidence.agent_name != self.target.qa_agent_name
            or not evidence.agent_id
            or not self.config.models.accepts(self.target.backend, evidence.model, self.env)
            or (self.env == "prod" and not evidence.channel_pinned)
        ):
            raise ValueError(
                "QA channel agent is not a verified cheap-model pin (Haiku for Claude)"
            )

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
        self.baseline_ids[message_id] = {str(m.get("id")) for m in baseline}
        self.driver.set_role("user")
        return message_id

    def collect(self, turn: Turn, timeout: float) -> None:
        # An end timestamp bounds evidence; only terminal stability proves it complete.
        turn.settled = False
        self._owned(turn.channel_id)
        self.driver.set_role("user")
        turn.guild_id = self.target.guild_id
        trigger_at = message_created_at({"id": turn.trigger_id}) or turn.started_at
        deadline = time.monotonic() + timeout
        stable_since: float | None = None
        previous = ""
        while time.monotonic() < deadline:
            # Sample acknowledgements before the slower message/thread reads.
            # Keep history: successful turns remove their transient reactions.
            trigger = obj(
                self.driver.call("GET", f"/channels/{turn.channel_id}/messages/{turn.trigger_id}")
            )
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
                    turn.trigger_reactions.append(reaction)
            reaction_elapsed = max(0, (utcnow() - trigger_at).total_seconds())
            turn.trigger_reaction_history.append(
                {
                    "elapsed_s": reaction_elapsed,
                    "reactions": list(turn.trigger_reactions),
                }
            )
            if turn.trigger_reactions and turn.first_visible_s is None:
                turn.first_visible_s = reaction_elapsed
                turn.first_visible_evidence = {
                    "source": "reaction_observed",
                    "elapsed_s": reaction_elapsed,
                }
            candidates = self.driver.turn_messages(
                turn.channel_id,
                after=turn.trigger_id,
                daimon_id=self.target.daimon_id,
            )
            turn.baseline_message_ids = sorted(self.baseline_ids.get(turn.trigger_id, set()))
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
            for message in messages:
                event = message_visible_at(
                    message,
                    trigger_at,
                    reused=str(message.get("id")) in self.baseline_ids.get(turn.trigger_id, set()),
                )
                if event:
                    timestamp, source = event
                    visible_s = max(0, (timestamp - trigger_at).total_seconds())
                    if turn.first_visible_s is None or visible_s < turn.first_visible_s:
                        turn.first_visible_s = visible_s
                        turn.first_visible_evidence = {
                            "source": source,
                            "message_id": str(message.get("id", "")),
                            "timestamp": timestamp.isoformat(),
                            "trigger_timestamp": trigger_at.isoformat(),
                            "elapsed_s": visible_s,
                        }
            header_pattern = r"^-#\s+" + re.escape(self.target.qa_agent_name or "") + r"\s*$"
            for message in messages if self.target.qa_agent_name else []:
                for match in re.finditer(
                    header_pattern, str(message.get("content") or ""), re.MULTILINE
                ):
                    header: Message = {
                        "message_id": str(message.get("id")),
                        "line": match[0].strip(),
                    }
                    if header not in turn.agent_subtext_headers:
                        turn.agent_subtext_headers.append(header)
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
                parent = (
                    turn.channel_id
                    if turn.channel_id in self.owned
                    else self.thread_parents.get(turn.channel_id)
                )
                if parent:
                    self.thread_parents[turn.thread_id] = parent
            turn.verdicts = [self.classify(m) for m in messages]
            for message, verdict in zip(messages, turn.verdicts, strict=True):
                if verdict == "working" and message.get("edited_timestamp"):
                    edit: Message = {
                        "message_id": message.get("id"),
                        "edited_timestamp": message.get("edited_timestamp"),
                        "phase": "running",
                    }
                    if edit not in turn.card_history:
                        turn.card_history.append(edit)
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
                    turn.settled = True
                    return
            else:
                turn.done_s = None
                stable_since = None
            previous = current
            time.sleep(min(self.config.poll_interval_s, max(0, deadline - time.monotonic())))
        turn.ended_at = utcnow()
        raise WatchTimeout("watch timed out; collected last messages, no terminal proof")

    def current_messages(self, turn: Turn) -> list[Message]:
        """Refresh only recorded bot messages inside this run's owned channels."""
        if not turn.messages:
            raise Pending("no recorded messages to refresh")
        rows: list[Message] = []
        for message in turn.messages:
            channel = str(message.get("channel_id") or turn.thread_id or turn.channel_id)
            self._owned(channel)
            identity = str(message.get("id") or "")
            if not identity:
                raise Pending("recorded message has no identity")
            row = obj(self.driver.call("GET", f"/channels/{channel}/messages/{identity}"))
            if str(row.get("id")) != identity:
                raise Pending("message refresh returned no matching evidence")
            rows.append(row)
        return rows

    def created_threads(self, turns: list[Turn]) -> set[str]:
        """Probe every trigger, including threads with no Daimon messages."""
        identities = {turn.thread_id for turn in turns if turn.thread_id}
        for turn in turns:
            self._owned(turn.channel_id)
            path = f"/channels/{turn.trigger_id}"
            try:
                row = obj(self.driver.call("GET", path))
            except SystemExit as exc:
                if str(exc).startswith(f"HTTP 404 on GET {path}:"):
                    continue
                raise Pending("thread inventory read unavailable") from None
            except Exception as exc:
                raise Pending("thread inventory read unavailable") from exc
            if (
                str(row.get("id")) != turn.trigger_id
                or row.get("type") not in {10, 11, 12}
                or str(row.get("parent_id")) != turn.channel_id
            ):
                raise Pending("thread inventory returned incomplete or mismatched evidence")
            identities.add(turn.trigger_id)
        return identities

    def channel_messages(self, turn: Turn) -> list[Message]:
        self._owned(turn.channel_id)
        if not turn.settled or turn.ended_at is None:
            raise Pending("channel history requires a settled reference turn")
        observation_start = turn.ended_at
        reference_ids = {str(row.get("id")) for row in turn.messages}
        observed: dict[str, Message] = {}
        for channel in self.owned | self.threads:
            self._owned(channel)
            rows = self.driver.messages(channel, after=None, limit=100)
            if len(rows) >= 100:
                raise Pending("channel history exceeds bounded observation")
            for row in rows:
                created = message_created_at(row)
                if (
                    row.get("type", 0) in {0, 19}
                    and (
                        obj(row.get("author")).get("id") == self.target.daimon_id
                        or str(row.get("application_id")) == self.target.daimon_id
                    )
                    and created is not None
                    and created > observation_start
                    and str(row.get("id")) not in reference_ids
                ):
                    observed[str(row.get("id"))] = row
        return list(observed.values())

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
            "allow_fail": step.allow_fail,
            "allow_fail_pattern": step.allow_fail_pattern,
            "allow_fail_exit_codes": step.allow_fail_exit_codes,
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
        if result.stdout.strip():
            data = cast(JsonValue, json.loads(result.stdout))
            exit_code = obj(data).get("exit_code", 0)
            if type(exit_code) is not int or exit_code < 0:
                raise ValueError("admin hook must return a nonnegative CLI exit_code")
            self.bindings["last_admin_exit_code"] = str(exit_code)
            if exit_code:
                output = obj(data).get("stdout", "")
                stderr = obj(data).get("stderr", "")
                if (
                    not step.allow_fail
                    or exit_code not in step.allow_fail_exit_codes
                    or not step.allow_fail_pattern
                    or not isinstance(output, str)
                    or not isinstance(stderr, str)
                    or not re.search(step.allow_fail_pattern, output + "\n" + stderr, re.MULTILINE)
                ):
                    raise RuntimeError(
                        "staging CLI command failed without expected refusal evidence"
                    )
                return
            bindings = obj(data).get("context", {})
            if not isinstance(bindings, dict) or any(
                not isinstance(v, str) for v in bindings.values()
            ):
                raise ValueError("admin hook context must be string bindings")
            self.bindings.update(cast(dict[str, str], bindings))

    def cli_read(self, command: str) -> str:
        import re
        import shlex

        if self.env != "staging":
            raise Pending("CLI readback is staging-only")
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise Pending("CLI readback command is malformed") from exc
        if (
            len(tokens) != 6
            or tokens[:3] != ["daimon", "agents", "--guild"]
            or tokens[3] != self.target.guild_id
            or tokens[4] != "get"
            or re.fullmatch(r"qa-[a-z0-9-]{1,96}", tokens[5]) is None
        ):
            raise Pending("CLI readback only supports this QA guild's agents get")
        hook = self.config.admin_hooks.get("cli_check")
        if not hook:
            raise Pending("read-only staging CLI hook is not configured")
        try:
            response = subprocess.run(
                hook,
                input=json.dumps(
                    {"guild_id": self.target.guild_id, "argv": tokens, "read_only": True}
                ),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Pending(f"read-only staging CLI hook unavailable: {type(exc).__name__}") from exc
        if response.returncode:
            raise Pending(f"read-only staging CLI hook exited {response.returncode}")
        if len(response.stdout.encode("utf-8")) > 131072:
            raise Pending("CLI readback exceeds bounded observation")
        return redact(response.stdout)

    def deployment_image(self) -> str:
        if self.env != "staging":
            raise Pending("deployment observation is staging-only")
        hook = self.target.deployment_probe
        if not hook:
            raise Pending("read-only staging deployment probe is not configured")
        try:
            response = subprocess.run(
                hook,
                input=json.dumps(
                    {"guild_id": self.target.guild_id, "env": "staging", "read_only": True}
                ),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if response.returncode:
                raise Pending("staging workers are not ready for observation")
            if len(response.stdout) > 16384:
                raise Pending("deployment probe exceeded its evidence bound")
            data = obj(cast(JsonValue, json.loads(response.stdout)))
            image = data.get("image")
            if not isinstance(image, str) or re.fullmatch(r"[a-f0-9]{40}", image) is None:
                raise Pending(
                    "deployment probe requires one verified full image SHA for all workers"
                )
            workers = obj(data.get("workers"))
            if set(workers) != {
                "daimon-discord-1",
                "daimon-slack-1",
                "daimon-teams-1",
                "daimon-scheduler-1",
            } or any(value != image for value in workers.values()):
                raise Pending("deployment probe found missing or mixed worker images")
            return image
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise Pending("read-only deployment probe unavailable") from exc

    def deployment_events(self, start: datetime, end: datetime, turns: list[Turn]) -> list[Message]:
        if self.env != "staging":
            return []
        delay = self.config.log_ingestion_delay_s - (utcnow() - end).total_seconds()
        if delay > 0:
            time.sleep(delay)
        message_ids = {
            str(message["id"]) for turn in turns for message in turn.messages if message.get("id")
        }
        events = [log_scope("event", "discord.draining")]
        if message_ids:
            messages = " OR ".join(log_scope("message_id", mid) for mid in sorted(message_ids))
            orphans = " OR ".join(
                log_scope("event", event)
                for event in (
                    "turn.card_orphan_retirement_issued",
                    "turn.card_orphan_retirement_completed",
                )
            )
            events.append(f"(({orphans}) AND ({messages}))")
        query = (
            f"timestamp>={json.dumps(start.isoformat())} AND "
            f"timestamp<={json.dumps(end.isoformat())} AND ({' OR '.join(events)})"
        )
        evidence: list[Message] = []
        for row in self._read_logs(query):
            payload = obj(row.get("jsonPayload"))
            event = payload.get("event")
            stamp = str(payload.get("timestamp") or row.get("timestamp") or "")
            try:
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise Pending("deployment event timestamp is unavailable") from exc
            if at.tzinfo is None or not start <= at <= end:
                continue
            if event == "discord.draining":
                affects = any(turn.started_at <= at <= (turn.ended_at or end) for turn in turns)
            elif (
                event
                in {"turn.card_orphan_retirement_issued", "turn.card_orphan_retirement_completed"}
                and str(payload.get("message_id")) in message_ids
            ):
                affects = True
            else:
                continue
            evidence.append(
                {
                    **{
                        key: payload[key]
                        for key in ("event", "rid", "thread_id", "message_id", "turn_id", "source")
                        if key in payload
                    },
                    "timestamp": stamp,
                    "affects_turn": affects,
                }
            )
        return evidence

    def logs(self, assertion: Assertion, turn: Turn) -> list[Message]:
        if not turn.thread_id or not turn.ended_at:
            raise Pending("logs require a thread id and bounded turn window")
        self._wait_for_logs(turn)
        window = " AND ".join(
            [
                f"timestamp>={json.dumps(turn.started_at.isoformat())}",
                f"timestamp<={json.dumps(turn.ended_at.isoformat())}",
            ]
        )
        thread_scope = log_scope("thread_id", turn.thread_id)
        anchors = [
            row
            for row in self._read_logs(f"{window} AND {thread_scope}")
            if str(obj(row.get("jsonPayload")).get("thread_id")) == turn.thread_id
        ]
        # Core preparation events carry rid but no thread_id. Discover that rid only
        # through this turn's thread-scoped logs, never through an unscoped event search.
        rids = {
            str(obj(row.get("jsonPayload"))["rid"])
            for row in anchors
            if obj(row.get("jsonPayload")).get("rid")
        }
        scope = " OR ".join([thread_scope, *[log_scope("rid", rid) for rid in sorted(rids)]])
        if assertion.kind == "log_absent" and not rids:
            raise Pending("no request-correlated logs; event absence is unproven")
        query = f"{window} AND ({scope}) AND {log_scope('event', assertion.event or '')}"
        rows = self._read_logs(query)
        return [
            row
            for row in rows
            if obj(row.get("jsonPayload")).get("event") == assertion.event
            and (
                str(obj(row.get("jsonPayload")).get("thread_id")) == turn.thread_id
                or str(obj(row.get("jsonPayload")).get("rid")) in rids
            )
            and all(
                obj(row.get("jsonPayload")).get(key) == value
                for key, value in assertion.fields.items()
            )
        ]

    def _wait_for_logs(self, turn: Turn) -> None:
        if turn.ended_at:
            delay = self.config.log_ingestion_delay_s - (utcnow() - turn.ended_at).total_seconds()
            if delay > 0:
                time.sleep(delay)

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
            decoded = cast(JsonValue, json.loads(result.stdout))
            if not isinstance(decoded, list) or any(not isinstance(row, dict) for row in decoded):
                raise ValueError("expected log record array")
            rows = objects(decoded)
        except ValueError as exc:
            raise Pending("Cloud Logging returned malformed evidence") from exc
        if len(rows) >= 1000:
            raise Pending("Cloud Logging results were truncated")
        for row in rows:
            wrapped = obj(row.get("jsonPayload")).get("message")
            if isinstance(wrapped, str):
                try:
                    payload = json.loads(wrapped)
                except ValueError as exc:
                    raise Pending("Cloud Logging contained malformed wrapped evidence") from exc
                if not isinstance(payload, dict):
                    raise Pending("Cloud Logging contained invalid wrapped evidence")
                row["jsonPayload"] = payload
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
        footer = self._footer_usage(turn)
        if self.env == "staging" and not turn.thread_id and not turn.messages and turn.ended_at:
            try:
                skipped = self._skipped_usage(turn)
            except Pending:
                skipped = None
            if skipped:
                return skipped
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
            if len(rows) == 1:
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
                    usd=float(str(row["cost_usd"]))
                    if row.get("cost_usd") is not None
                    else footer.usd,
                    source="turn_outcomes+footer_rounded"
                    if row.get("cost_usd") is None and footer.usd is not None
                    else "turn_outcomes",
                    models=[str(m) for m in models] if isinstance(models, list) else [],
                )
        except Exception:
            pass
        return footer

    def _skipped_usage(self, turn: Turn) -> Usage | None:
        """Positive pre-admission evidence only; silence never proves a skip."""
        if not turn.ended_at or turn.guild_id != self.target.guild_id:
            return None
        self._wait_for_logs(turn)
        event = "turn.skipped.writers_none"
        query = " AND ".join(
            [
                f"timestamp>={json.dumps(turn.started_at.isoformat())}",
                f"timestamp<={json.dumps(turn.ended_at.isoformat())}",
                log_scope("event", event),
                log_scope("guild_id", turn.guild_id),
                log_scope("channel_id", turn.channel_id),
            ]
        )
        for row in self._read_logs(query):
            payload = obj(row.get("jsonPayload"))
            stamp = str(payload.get("timestamp") or row.get("timestamp") or "")
            try:
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if (
                payload.get("event") == event
                and str(payload.get("guild_id")) == turn.guild_id
                and str(payload.get("channel_id")) == turn.channel_id
                and at.tzinfo is not None
                and turn.started_at <= at <= turn.ended_at
            ):
                return Usage(
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    usd=0,
                    source="pre-admission skip log",
                    skipped_reason=event,
                    skip_evidence={
                        "event": event,
                        "guild_id": turn.guild_id,
                        "channel_id": turn.channel_id,
                        "timestamp": stamp,
                    },
                )
        return None

    @staticmethod
    def _footer_usage(turn: Turn) -> Usage:
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

    def thread_name(self, turn: Turn) -> str:
        if not turn.thread_id:
            raise Pending("no observed thread for name readback")
        self._owned(turn.thread_id)
        self.driver.set_role("user")
        metadata = obj(self.driver.call("GET", f"/channels/{turn.thread_id}"))
        parent = self.thread_parents.get(turn.thread_id)
        if (
            str(metadata.get("id")) != turn.thread_id
            or metadata.get("guild_id") != self.target.guild_id
            or metadata.get("parent_id") != parent
            or parent not in self.owned
            or metadata.get("type") != 11
            or not isinstance(metadata.get("name"), str)
        ):
            raise Pending("thread name readback did not match the owned QA thread")
        return str(metadata["name"])

    def classify(self, message: Message) -> str:
        return self.driver.classify(message)

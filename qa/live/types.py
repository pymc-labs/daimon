"""Evidence shared by execution, evaluation, reporting and fake backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from pydantic import JsonValue

from qa.live.schema import Assertion, Status, Step

Message = dict[str, JsonValue]


def utcnow() -> datetime:
    return datetime.now(UTC)


def obj(value: JsonValue) -> Message:
    return value if isinstance(value, dict) else {}


def objects(value: JsonValue) -> list[Message]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def text_components(message: Message, *, include_fields: bool = True) -> list[str]:
    parts = [str(message.get("content") or "")]
    for embed in objects(message.get("embeds")):
        parts.extend(str(embed.get(k) or "") for k in ("title", "description"))
        parts.append(str(obj(embed.get("footer")).get("text") or ""))
        for field_ in objects(embed.get("fields")) if include_fields else []:
            parts.extend(str(field_.get(k) or "") for k in ("name", "value"))
    return parts


def text_of(message: Message, *, include_fields: bool = True) -> str:
    return "\n".join(text_components(message, include_fields=include_fields))


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    usd: float | None = None
    source: str = "unavailable"
    models: list[str] = field(default_factory=list[str])
    skipped_reason: str | None = None
    skip_evidence: Message = field(default_factory=dict[str, JsonValue])


@dataclass
class Turn:
    number: int
    trigger_id: str
    channel_id: str
    started_at: datetime
    ended_at: datetime | None = None
    first_visible_s: float | None = None
    first_visible_evidence: Message = field(default_factory=dict[str, JsonValue])
    done_s: float | None = None
    thread_id: str | None = None
    progress_seen_s: float | None = None
    guild_id: str = "1435062989119295640"
    trigger_reactions: list[Message] = field(default_factory=list[Message])
    trigger_reaction_history: list[Message] = field(default_factory=list[Message])
    messages: list[Message] = field(default_factory=list[Message])
    agent_subtext_headers: list[Message] = field(default_factory=list[Message])
    channel_history: list[Message] = field(default_factory=list[Message])
    parent_messages: list[Message] = field(default_factory=list[Message])
    verdicts: list[str] = field(default_factory=list[str])
    usage: Usage = field(default_factory=Usage)
    settled: bool = False

    @property
    def text(self) -> str:
        return "\n".join(text_of(m) for m in self.messages)


@dataclass
class Check:
    kind: str
    status: Status
    reason: str
    turn: int | None = None
    evidence: list[str] = field(default_factory=list[str])


@dataclass
class DeploymentEvidence:
    started_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None
    start_image: str | None = None
    end_image: str | None = None
    events: list[Message] = field(default_factory=list[Message])
    interrupted: bool = False
    end_probe_pending: bool = False
    settled_image: str | None = None
    events_error: str | None = None
    error: str | None = None


class Pending(RuntimeError):
    """Unavailable capability; must never become a passing assertion."""


class WatchTimeout(Pending):
    """Observation completed without terminal proof; evaluate the collected evidence."""


class Backend(Protocol):
    fallback_watch_s: float

    def context(self) -> dict[str, str]: ...
    def preflight(self, roles: set[str]) -> None: ...
    def create_channel(self, name: str) -> str: ...
    def verify_model(self, channel: str) -> None: ...
    def delete_channel(self, channel: str) -> None: ...
    def send(
        self,
        channel: str,
        step: Step,
        *,
        mention: bool,
        reply_message_id: str | None = None,
    ) -> str: ...
    def collect(self, turn: Turn, timeout: float) -> None: ...
    def react(self, channel: str, message: str, emoji: str) -> None: ...
    def admin(self, step: Step, channel: str) -> None: ...
    def logs(self, assertion: Assertion, turn: Turn) -> list[Message]: ...
    def db_check(self, sql: str, turn: Turn | None = None) -> JsonValue: ...
    def usage(self, turn: Turn) -> Usage: ...
    def thread_name(self, turn: Turn) -> str: ...
    def channel_messages(self, turn: Turn) -> list[Message]: ...
    def cli_read(self, command: str) -> str: ...
    def classify(self, message: Message) -> str: ...
    def deployment_image(self) -> str: ...
    def deployment_events(
        self, start: datetime, end: datetime, turns: list[Turn]
    ) -> list[Message]: ...


class Judge(Protocol):
    usage: list[Usage]
    errors: list[Message]

    def evaluate(self, rubric: str, answer: str) -> tuple[bool, str]: ...

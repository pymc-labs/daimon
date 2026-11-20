"""Thread to cases: one candidate per answered requester turn.

A `ThreadSource` reads a chat thread into a `ThreadRecord`. `build_cases` turns each answered
turn (people's messages followed by the agent's answer) into a case: the messages before it are
history, the world holds only the calls made for that turn, and fidelity says what the record
holds. Every case is scrubbed before it leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, Protocol

from daimon.core.evals.blobs import ref_nodes
from daimon.core.evals.format import (
    Attachment,
    BlobRef,
    ChatMessage,
    Clock,
    Exchange,
    ExchangeRequest,
    ExchangeResponse,
    Fidelity,
    Group,
    JsonObject,
    Participant,
    Place,
    Platform,
    Provenance,
    Reference,
    Surface,
    Tool,
    Turn,
    World,
    sha256_hex,
    to_document,
)
from daimon.core.evals.scrub.bundle import ScrubbedBlob, scrub_blob, scrub_case
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.entities import KnownEntities, NamedEntry, PersonEntry
from daimon.core.evals.scrub.fakes import keyed_hash
from daimon.core.evals.scrub.sweep import Leftover
from pydantic import BaseModel, ConfigDict, Field, JsonValue

CONVERTER = Tool(name="daimon.evals.from_thread", version="1")
_TITLE_CHARS = 80
GroupKind = Literal["discord_role", "slack_user_group", "teams_team", "web_group"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThreadFile(_Model):
    name: str
    media_type: str = "application/octet-stream"
    data: bytes | None = None
    """None when the source could not read the file: the case says so."""


class ThreadCall(_Model):
    """One tool call the agent made for a turn."""

    tool: str
    args: JsonValue = None
    result: JsonValue = None
    has_result: bool = True
    is_error: bool = False
    write: bool = False
    server: str | None = None
    service: str | None = None


class ThreadPerson(_Model):
    id: str
    name: str
    handle: str | None = None
    email: str | None = None
    role: Literal["owner", "admin", "member", "visitor", "guest", "external"] = "member"
    groups: list[str] = Field(default_factory=list[str])
    is_bot: bool = False


class ThreadMessage(_Model):
    id: str
    author: str
    at: datetime
    text: str = ""
    files: list[ThreadFile] = Field(default_factory=list[ThreadFile])
    calls: list[ThreadCall] = Field(default_factory=list[ThreadCall])
    """For a person's message: what the agent called while answering it."""


class ThreadRecord(_Model):
    platform: Platform
    ref: str
    """A stable name for the thread (the link, or an id); only a keyed hash of it is kept."""
    conversation: Literal["dm", "group_dm", "channel", "thread"] = "thread"
    title: str | None = None
    workspace: Place | None = None
    channel: Place | None = None
    thread: Place | None = None
    people: list[ThreadPerson]
    """Everyone in the thread, the agent included (`is_bot`)."""
    messages: list[ThreadMessage]
    events_recorded: bool = False
    """True when the source holds the agent's tool calls, so a turn without any made none."""

    def entities(self) -> KnownEntities:
        """What this thread itself says is private: its people and places."""
        places = [p for p in (self.workspace, self.channel, self.thread) if p]
        return KnownEntities(
            people=[
                PersonEntry(
                    name=p.name,
                    emails=[p.email] if p.email else [],
                    handles=[p.handle] if p.handle else [],
                    ids=[p.id],
                )
                for p in self.people
                if not p.is_bot
            ],
            channels=[NamedEntry(name=p.name, aliases=[p.id]) for p in places if p.name],
            ids=[p.id for p in places],
        )


class ThreadNotFound(LookupError):
    """The thread does not exist, or the reader may not see it."""


class ThreadSource(Protocol):
    """Reads one thread, for whoever asks. `ref` is a link or an id the source understands."""

    async def thread(self, ref: str) -> ThreadRecord: ...


@dataclass
class BuiltCase:
    case: JsonObject
    blobs: dict[str, ScrubbedBlob] = field(default_factory=dict[str, ScrubbedBlob])
    leftovers: list[Leftover] = field(default_factory=list[Leftover])


@dataclass(frozen=True)
class _Turn:
    index: int
    asked: list[ThreadMessage]
    answered: list[ThreadMessage]


def _turns(record: ThreadRecord) -> list[_Turn]:
    """Runs of people's messages that the agent answered, in order."""
    bots = {p.id for p in record.people if p.is_bot}
    turns: list[_Turn] = []
    asked: list[ThreadMessage] = []
    answer: list[ThreadMessage] = []
    start = 0
    for position, message in enumerate(record.messages):
        if message.author in bots:
            if asked:
                answer.append(message)
            continue
        if answer:
            turns.append(_Turn(start, asked, answer))
            asked, answer = [], []
        if not asked:
            start = position
        asked.append(message)
    if asked and answer:
        turns.append(_Turn(start, asked, answer))
    return turns


def _title(text: str) -> str:
    line = next((x.strip() for x in text.splitlines() if x.strip()), "")
    return (line[:_TITLE_CHARS] or "Untitled thread turn").strip()


def _message(message: ThreadMessage, attachments: list[Attachment]) -> ChatMessage:
    return ChatMessage(
        id=message.id, author=message.author, at=message.at, text=message.text,
        attachments=attachments or None,
    )  # fmt: skip


def _participant(person: ThreadPerson, *, requester: bool) -> Participant:
    return Participant(
        id=person.id, display_name=person.name, handle=person.handle, email=person.email,
        role=person.role, groups=person.groups or None, is_requester=requester,
        is_bot=person.is_bot,
    )  # fmt: skip


def _exchanges(calls: list[ThreadCall]) -> list[Exchange]:
    return [
        Exchange(
            id=f"x{n}", surface="mcp", service=c.service, server=c.server, tool=c.tool,
            request=ExchangeRequest(args=c.args),
            response=ExchangeResponse(
                result=c.result if c.has_result else None, is_error=c.is_error
            ),
            kind="write" if c.write else "read", origin="recorded", order=n,
        )
        for n, c in enumerate(calls, start=1)
    ]  # fmt: skip


def _fidelity(record: ThreadRecord, calls: list[ThreadCall], files_missing: bool) -> Fidelity:
    missing: list[str] = []
    if not record.events_recorded:
        level = "text"
        missing += ["tool_calls", "tool_results"]
    elif any(not c.has_result for c in calls):
        level = "tools"
        missing.append("tool_results")
    else:
        level = "full"
    if files_missing:
        missing.append("files")
    return Fidelity(level=level, missing=missing or None)


def build_cases(record: ThreadRecord, scrubber: Scrubber) -> list[BuiltCase]:
    """One scrubbed case per answered turn of `record`. Nothing real is left in them."""
    people = {p.id: p for p in record.people}
    bot = next((p for p in record.people if p.is_bot), None)
    if bot is None:
        raise ValueError("the thread has no agent")
    cache: dict[str, tuple[str, ScrubbedBlob]] = {}
    built: list[BuiltCase] = []

    def attachments(message: ThreadMessage, blobs: dict[str, ScrubbedBlob]) -> list[Attachment]:
        out: list[Attachment] = []
        for n, f in enumerate(message.files, start=1):
            ident = f"{message.id}-{n}"
            if f.data is None:
                out.append(
                    Attachment(id=ident, name=f.name, media_type=f.media_type, state="missing")
                )
                continue
            raw = sha256_hex(f.data)
            if raw not in cache:
                done = scrub_blob(scrubber, f.data, f.media_type, f.name)
                cache[raw] = (sha256_hex(done.data), done)
            sha, done = cache[raw]
            blobs[sha] = done
            state: Literal["present", "rebuilt", "redacted"] = (
                "rebuilt"
                if done.scrub == "rebuilt"
                else "redacted"
                if done.scrub == "redacted"
                else "present"
            )
            out.append(
                Attachment(
                    id=ident, name=f.name, media_type=done.media_type, size=len(done.data),
                    blob=BlobRef(
                        sha256=sha, size=len(done.data), media_type=done.media_type, name=f.name
                    ),
                    state=state,
                )
            )  # fmt: skip
        return out

    for turn in _turns(record):
        blobs: dict[str, ScrubbedBlob] = {}
        last = turn.asked[-1]
        before = record.messages[: turn.index]
        history = [_message(m, attachments(m, blobs)) for m in before]
        asked = [_message(m, attachments(m, blobs)) for m in turn.asked]
        calls = [c for m in turn.asked for c in m.calls]
        files_missing = any(f.data is None for m in (*before, *turn.asked) for f in m.files)
        speakers = {m.author for m in (*before, *turn.asked)} | {last.author}
        participants = [
            _participant(p, requester=p.id == last.author)
            for p in record.people
            if not p.is_bot and (p.id in speakers or p.id == last.author)
        ]
        if last.author not in people:
            raise ValueError("a message is by someone the thread does not list")
        groups = sorted({g for p in participants for g in p.groups or []})
        surface = Surface(
            platform=record.platform, conversation=record.conversation,
            workspace=record.workspace, channel=record.channel, thread=record.thread,
            participants=participants, bot=_participant(bot, requester=False),
            groups=[Group(id=g, name=g, kind=_GROUP_KIND[record.platform]) for g in groups] or None,
        )  # fmt: skip
        answer = "\n\n".join(m.text for m in turn.answered if m.text)
        case_id = "c-" + keyed_hash(scrubber.key, "case", f"{record.ref}\0{last.id}")[:16]
        flags: list[JsonValue] = [{"reason": "expectations_unreviewed"}]
        if files_missing:
            flags.append({"reason": "world_incomplete"})
        document: JsonObject = {
            "schema": "daimon.eval.case/1",
            "id": case_id,
            "revision": 1,
            "title": _title(last.text or record.title or ""),
            "tags": ["from-thread", record.platform],
            "source_thread": record.ref,
            "source_turn": len([t for t in _turns(record) if t.index <= turn.index]),
            **to_document_fields(
                fidelity=_fidelity(record, calls, files_missing),
                provenance=Provenance(
                    kind="recorded",
                    source_system=record.platform,
                    source_ref=record.ref,
                    recorded_at=last.at,
                    converter=CONVERTER,
                ),
                surface=surface,
                clock=Clock(now=last.at),
                turn=Turn(
                    messages=asked, trigger="dm" if record.conversation == "dm" else "mention"
                ),
                world=World(exchanges=_exchanges(calls) or None),
            ),
            "history": [to_document(m) for m in history],
            "expect": {"reference": to_document(Reference(text=answer))} if answer else {},
            "review_flags": flags,
        }
        reasons = {sha: b.reason for sha, b in blobs.items() if b.reason}
        done, leftovers = scrub_case(scrubber, document, blob_reasons=reasons)
        built.append(
            BuiltCase(
                done,
                {
                    str(n["sha256"]): blobs[str(n["sha256"])]
                    for n in ref_nodes(done)
                    if str(n["sha256"]) in blobs
                },
                leftovers,
            )
        )
    return built


_GROUP_KIND: Final[dict[str, GroupKind]] = {
    "discord": "discord_role", "slack": "slack_user_group", "teams": "teams_team",
    "web": "web_group",
}  # fmt: skip


def to_document_fields(**models: object) -> JsonObject:
    out: JsonObject = {}
    for key, model in models.items():
        assert isinstance(model, BaseModel)
        out[key] = to_document(model)
    return out

"""Who must be able to read what a data tool returns: one rule for every path. Pure.

The shell turns each path into a `DataOrigin` from signed facts only (the caller's
credential and its turn origin, never the model): a room or shared thread with the
people who can view it, a private place (a DM, web chat, the CLI, a personal key), an
agent asking a data-reading agent, or a place with no person to answer for. The answer
is bounded by everyone who will see it: the agent's owner, the asker and every viewer.

- A contained room (outsiders may read it, or nobody can say), a turn held for an
  outsider, and a place with no person get no data at all.
- The scope ceiling is the intersection of everyone's scopes; a guest (a viewer with no
  linked account) caps it at `public`, and so do unknown, empty or uncounted viewers and
  an asker who is not among the viewers.
- Access-list rows are read as every person and guest named, and only when the agent
  has an owner, the asker is a known person among the viewers, and at most `CAP` are named.
- A guest, a person marked redacted, or a `public` cap turns on read-time redaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import reduce
from typing import Final, Literal

#: People and guests one audience may name; more, or none, read no access-list rows.
CAP: Final = 200
PUBLIC: Final = "public"

OriginKind = Literal["room", "private", "agent_request", "none"]
RefusalReason = Literal["no_person", "contained", "agent_request"]


def subject(provider: str, workspace: str, kind: str, id_: str) -> str:
    """A source principal: `<provider>/<workspace>:<kind>:<id>` (kind: user, group, role...)."""
    return f"{provider}/{workspace}:{kind}:{id_}"


def user_subject(platform: str, workspace: str, user_id: str) -> str:
    """A platform user as a reader, in the form its mirrored identity takes: `workspace` is
    the tenant's external id (a Discord server, a Slack team)."""
    return subject(platform, workspace, "user", user_id)


@dataclass(frozen=True)
class Reader:
    """Someone who reads the answer: a platform subject, and the account it is linked to
    (None: a guest)."""

    subject: str
    account_id: uuid.UUID | None = None


@dataclass(frozen=True)
class DataOrigin:
    """Where a data call's answer goes, as the shell resolved it from signed facts.

    `viewers` is everyone who can view a room or shared thread; None when unknown or
    more than `CAP` (they cannot all be named). `contained` is a room outsiders may read,
    or a turn held for an outsider. `caller_reads_data` is, for an agent asking a
    data-reading agent, whether the asking agent reads data in the same room.
    """

    kind: OriginKind
    asker: Reader | None = None
    viewers: frozenset[Reader] | None = frozenset()
    contained: bool = False
    caller_reads_data: bool = False


@dataclass(frozen=True)
class DataAudience:
    """Everyone the answer must be readable by. `people` bound the scopes."""

    people: frozenset[uuid.UUID]
    guests: frozenset[str]
    public_only: bool
    list_rows: bool
    redact: bool


@dataclass(frozen=True)
class DataRefusal:
    """No data tool runs here."""

    reason: RefusalReason


def data_audience(
    origin: DataOrigin,
    *,
    agent_owner: uuid.UUID | None,
    redacted: frozenset[uuid.UUID] = frozenset(),
) -> DataAudience | DataRefusal:
    """The audience a data call answers for at `origin`, or why none may run."""
    if origin.kind == "none":
        return DataRefusal("no_person")
    if origin.contained:
        return DataRefusal("contained")
    if origin.kind == "agent_request" and not origin.caller_reads_data:
        return DataRefusal("agent_request")
    asker = origin.asker
    readers = {asker} if asker is not None else set[Reader]()
    counted = origin.kind == "private" or bool(origin.viewers)
    if origin.kind != "private" and origin.viewers:
        readers |= origin.viewers
    people = {r.account_id for r in readers if r.account_id is not None}
    if agent_owner is not None:
        people.add(agent_owner)
    guests = frozenset(r.subject for r in readers if r.account_id is None)
    asker_reads = (
        asker is not None
        and asker.account_id is not None
        and (origin.kind == "private" or (origin.viewers is not None and asker in origin.viewers))
    )
    public_only = bool(guests) or not counted or not asker_reads
    return DataAudience(
        people=frozenset(people),
        guests=guests,
        public_only=public_only,
        list_rows=agent_owner is not None
        and asker_reads
        and counted
        and len(people) + len(guests) <= CAP,
        redact=public_only or bool(people & redacted),
    )


def scope_ceiling(
    audience: DataAudience, scopes_of: Mapping[uuid.UUID, frozenset[str]]
) -> frozenset[str]:
    """The scopes every person in `audience` holds (a person with none listed holds none),
    capped at `public` when the audience says so."""
    held = [scopes_of.get(person, frozenset[str]()) for person in audience.people]
    ceiling = reduce(frozenset[str].intersection, held) if held else frozenset[str]()
    return ceiling & {PUBLIC} if audience.public_only else ceiling

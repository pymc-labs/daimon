"""Redaction rules: what a source's text loses as it is ingested, and which names a reader
who must not see them never gets.

A rule set is a JSON or YAML document, named per tenant:

    aliases:     {"Alice": "an engineer", "Acme": "client A"}   # term -> what it becomes
    removals:    ["internal-codename"]                           # replaced by [removed]
    regex:       [{pattern: "ticket-\\d+", replace: "a ticket"}]
    exclude_ids: ["doc:42"]                                      # records never stored
    entities:    [{name: "Acme", aliases: ["Acme Inc"], scope: "client:acme", label: "[client]"}]

`at` says when a set applies. Unset, as before: at ingest a source applies its set's regex,
aliases and removals to every record's text before it is chunked or embedded, and `entities`
apply at read time. `at: storage` applies all of them at ingest, entity names becoming their
label for everyone. `at: answer` stores text as the source has it and applies regex, aliases
and removals to every answer, and entities as below. `exclude_ids` always drops records at
ingest, so they are never stored. A replacement that starts with an article
("an engineer") takes the place of a preceding "the", "a" or "an" and is capitalised at the
start of a sentence. Terms of four letters or more match in any case. Read-time `entities`:
when the audience holds a guest or a person marked redacted, an entity's names become its
label unless the audience holds its `scope`.

A set is inline (declared) or a repo file pinned by blob sha. Only the approved sha counts:
when the file upstream changes, its new rules wait as `pending` and every source using the set
stops syncing until a data admin approves that sha (`approve`).
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import re
import uuid
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, cast

import httpx
import structlog
import yaml
from daimon.core.config import Settings
from daimon.data import audit, identity, pool
from daimon.data.schema import Schemas
from daimon.data.sync.connector import ItemRecord
from daimon.data.sync.credentials import resolve_ref
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = structlog.get_logger()

KEY: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"
REMOVED: Final = "[removed]"
HIDDEN: Final = "[redacted]"
ANYCASE_MIN: Final = 4
"""Terms at least this long match in any case; shorter ones only as written."""
API: Final = "https://api.github.com"
LIMIT: Final = 10_000
"""Most terms of one kind in a set."""

transport: httpx.AsyncBaseTransport | None = None
"""Tests answer for the repo host here."""

_ARTICLE = re.compile(r"(?i)(?:the|an?)\s")
_SENTENCE = re.compile(r"(?:^|[.!?:]\s+|\n\s*)$")


class NotApproved(Exception):
    """A source's rule set may not be applied yet. The message holds no rule."""


# ---- the rules -----------------------------------------------------------------------------

Term = Field(min_length=2, max_length=200)


# The stdlib's own reading of a pattern; private, so untyped.
sre: Any = importlib.import_module("re._parser")
_ATOMS = {sre.LITERAL, sre.NOT_LITERAL, sre.IN, sre.ANY}
_REPEATS = {sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT}


def _unsafe(value: object) -> str | None:
    """Why a parsed pattern may backtrack without end: a repeat of more than one character or
    class, like `(a|aa)+`, or a back reference. None when it is safe."""
    if isinstance(value, sre.SubPattern):
        for op, av in cast("Iterable[tuple[object, Any]]", value):
            if op in (sre.GROUPREF, sre.GROUPREF_EXISTS):
                return "a pattern can't refer back to a group"
            if op in _REPEATS and (len(body := av[2]) != 1 or body[0][0] not in _ATOMS):
                return "only a single character or class can repeat, never a group"
            if why := _unsafe(av):
                return why
    elif isinstance(value, tuple | list):
        return next(filter(None, map(_unsafe, cast("Iterable[object]", value))), None)
    return None


class Pattern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern: str = Field(min_length=1, max_length=500)
    replace: str = ""

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as e:
            raise ValueError(f"not a regular expression: {e.msg}") from None
        if why := _unsafe(sre.parse(value)):
            raise ValueError(why)
        return value


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=200)
    aliases: list[str] = Field(default=[], max_length=100)
    scope: str | None = None
    """Readers who hold this scope see the entity's names."""
    label: str = Field(default=HIDDEN, max_length=50)


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: Literal["storage", "answer"] | None = None
    """When the set applies (module docstring); None keeps text at ingest, names at read."""
    aliases: dict[str, str] = Field(default={}, max_length=LIMIT)
    removals: list[str] = Field(default=[], max_length=LIMIT)
    regex: list[Pattern] = Field(default=[], max_length=1000)
    exclude_ids: list[str] = Field(default=[], max_length=LIMIT)
    entities: list[Entity] = Field(default=[], max_length=LIMIT)

    @field_validator("aliases")
    @classmethod
    def _terms(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not 2 <= len(t) <= 200 for t in value):
            raise ValueError("a term is 2 to 200 characters")
        return value

    @field_validator("removals")
    @classmethod
    def _removals(cls, value: list[str]) -> list[str]:
        if any(not 2 <= len(t) <= 200 for t in value):
            raise ValueError("a term is 2 to 200 characters")
        return value


def parse(text: str | bytes) -> Rules:
    """Rules from a JSON or YAML document; `ValueError` naming locations, never the input."""
    try:
        raw: object = yaml.safe_load(text)  # pyright: ignore[reportUnknownMemberType]
        return Rules.model_validate(raw)
    except yaml.YAMLError:
        raise ValueError("rules are not valid YAML or JSON") from None
    except ValidationError as e:
        raise ValueError(_where(e)) from None


def _where(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())


# ---- applying them -------------------------------------------------------------------------


def _alt(terms: Iterable[str]) -> str:
    ordered = sorted(set(terms), key=len, reverse=True)
    return "|".join(
        f"(?i:{re.escape(t)})" if len(t) >= ANYCASE_MIN else re.escape(t) for t in ordered
    )


def walk[T](value: T, fn: Callable[[str], str]) -> T:
    """`value` (JSON-like) with `fn` applied to every string value; keys are left."""
    out: object = value
    if isinstance(value, str):
        out = fn(value)
    elif isinstance(value, dict):
        out = {k: walk(v, fn) for k, v in cast("dict[object, object]", value).items()}
    elif isinstance(value, list | tuple):
        out = [walk(v, fn) for v in cast("Iterable[object]", value)]
    return cast("T", out)


class Redactor:
    """What a source applies to its records as it ingests them, or, for an `answer` set,
    what every answer goes through (`answer_redactor`)."""

    def __init__(self, rules: Rules, *, answer: bool = False) -> None:
        if rules.at == "answer" and not answer:
            rules = Rules(exclude_ids=rules.exclude_ids)  # only what is never stored
        self._names = Entities(rules.entities, ()) if rules.at == "storage" else None
        self._regex = [(re.compile(p.pattern), p.replace) for p in rules.regex]
        self._ids = frozenset(rules.exclude_ids)
        self._aliases = dict(rules.aliases)
        self._folded: dict[str, str] = {}
        for term, rep in self._aliases.items():
            self._folded.setdefault(term.casefold(), rep)
        self._alias_re = (
            re.compile(
                r"(?<!\w)(?i:(?P<art>(?:the|an?)\s+))?(?P<term>" + _alt(self._aliases) + r")(?!\w)"
            )
            if self._aliases
            else None
        )
        self._removal_re = (
            re.compile(r"(?<!\w)(?:" + _alt(rules.removals) + r")(?!\w)")
            if rules.removals
            else None
        )

    def excludes(self, external_id: str) -> bool:
        return external_id in self._ids

    def text(self, s: str) -> str:
        for rx, rep in self._regex:
            s = rx.sub(rep, s)
        if self._alias_re:
            s = self._alias_re.sub(lambda m: self._alias(s, m), s)
        if self._removal_re:
            s = self._removal_re.sub(REMOVED, s)
        return s if self._names is None else self._names.text(s)

    def __call__[T](self, value: T) -> T:
        return walk(value, self.text)

    def _alias(self, s: str, m: re.Match[str]) -> str:
        term = m["term"]
        rep = self._aliases.get(term) or self._folded[term.casefold()]
        article = m["art"]
        if not _ARTICLE.match(rep):
            return (article or "") + rep
        if _SENTENCE.search(s[: m.start()]):
            return rep[0].upper() + rep[1:]
        return rep

    def record(self, r: ItemRecord) -> ItemRecord:
        """`r` with its text redacted. The payload is the connector's own and is left."""
        text = self.text
        return replace(
            r,
            title=text(r.title),
            body=text(r.body),
            author=None if r.author is None else text(r.author),
            container=None if r.container is None else text(r.container),
            client=None if r.client is None else text(r.client),
            facets=walk(r.facets, text),
        )


class Entities:
    """What a reader is shown instead of the names of entities they may not see."""

    def __init__(self, entities: Iterable[Entity], held: Collection[str]) -> None:
        self._label: dict[str, str] = {}
        for e in entities:
            if e.scope is None or e.scope not in held:
                for name in (e.name, *e.aliases):
                    if len(name) >= 3:
                        self._label.setdefault(name.casefold(), e.label)
        names = sorted(self._label, key=len, reverse=True)
        self._rx = (
            re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, names)) + r")(?!\w)", re.I)
            if names
            else None
        )

    def text(self, s: str) -> str:
        if self._rx is None:
            return s
        return self._rx.sub(lambda m: self._label[m.group(0).casefold()], s)

    def __call__[T](self, value: T) -> T:
        return walk(value, self.text)


# ---- the stored sets -----------------------------------------------------------------------


async def ingest_redactor(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, name: str
) -> Redactor:
    """The redactor a source with set `name` applies; `NotApproved` unless its rules are
    approved and, for a pinned file, still at the approved sha."""
    cur = await conn.execute(
        f"select rules, repo, pin_sha, approved_sha from {schemas.data}.redaction_sets"
        " where tenant_id = %s and name = %s".encode(),
        (tenant_id, name),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotApproved("its redaction set does not exist")
    if row["rules"] is None:
        raise NotApproved("its redaction rules are not approved yet")
    if row["repo"] is not None and row["pin_sha"] != row["approved_sha"]:
        raise NotApproved("its redaction rules changed upstream and await approval")
    return Redactor(Rules.model_validate(row["rules"]))


async def read_redactor(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, held: Collection[str]
) -> Entities:
    """The names every approved set of the tenant hides from a reader who holds `held`."""
    cur = await conn.execute(
        f"select rules from {schemas.data}.redaction_sets where tenant_id = %s"
        " and rules is not null order by name".encode(),
        (tenant_id,),
    )
    rules = [Rules.model_validate(r["rules"]) for r in await cur.fetchall()]
    return Entities((e for r in rules if r.at != "storage" for e in r.entities), held)


async def answer_redactor(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID
) -> Redactor | None:
    """The regex, aliases and removals of the tenant's approved `answer` sets, which every
    answer goes through; None when it has none."""
    cur = await conn.execute(
        f"select rules from {schemas.data}.redaction_sets where tenant_id = %s"
        " and rules ->> 'at' = 'answer' order by name".encode(),
        (tenant_id,),
    )
    sets = [Rules.model_validate(r["rules"]) for r in await cur.fetchall()]
    if not sets:
        return None
    merged = Rules(
        at="answer",
        aliases={k: v for r in sets for k, v in r.aliases.items()},
        removals=[t for r in sets for t in r.removals],
        regex=[p for r in sets for p in r.regex],
    )
    return Redactor(merged, answer=True)


async def redacted_among(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    accounts: Collection[uuid.UUID],
) -> frozenset[uuid.UUID]:
    """Those of `accounts` marked redacted in this tenant."""
    cur = await conn.execute(
        f"select distinct account_id from {schemas.data}.identities where tenant_id = %s"
        " and redacted and account_id = any(%s)".encode(),
        (tenant_id, list(accounts)),
    )
    return frozenset(r["account_id"] for r in await cur.fetchall())


async def restart_sources(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, name: str
) -> None:
    """Sync every source using set `name` again from the start, so changed rules reach what
    is stored."""
    await conn.execute(
        f"update {schemas.data}.source_cursors set state = '{{}}', requested_at = now()"
        f" where source_id in (select id from {schemas.data}.sources"
        " where tenant_id = %s and redaction_set = %s)".encode(),
        (tenant_id, name),
    )


async def hold(
    conn: AsyncConnection[DictRow], schemas: Schemas, source_id: uuid.UUID, why: str
) -> None:
    """Show on the source why it is not syncing."""
    await conn.execute(
        f"insert into {schemas.data}.source_cursors (source_id, last_status, last_error)"
        " values (%s, 'error', %s) on conflict (source_id) do update set"
        " last_status = excluded.last_status, last_error = excluded.last_error".encode(),
        (source_id, why),
    )


# ---- pinned files --------------------------------------------------------------------------


def blob_sha(data: bytes) -> str:
    """A file's git blob sha."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


async def _fetch(row: DictRow) -> tuple[str, Rules]:
    """The pinned file's blob sha and rules, as the repo host serves them now."""
    token = resolve_ref(row["token_ref"]) if row["token_ref"] else None
    if row["token_ref"] and not token:
        raise ValueError("the token cannot be read")
    headers = {
        "Accept": "application/vnd.github+json",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    }
    params = {"ref": row["ref"]} if row["ref"] else {}
    async with httpx.AsyncClient(timeout=20, transport=transport) as http:
        resp = await http.get(
            f"{API}/repos/{row['repo']}/contents/{row['path']}", params=params, headers=headers
        )
    if resp.status_code != 200:
        raise ValueError(f"the repo host answered {resp.status_code}")
    body = cast("dict[str, Any]", resp.json())
    if body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
        raise ValueError("the file is not a plain file within the host's size limit")
    data = base64.b64decode(cast(str, body["content"]))
    sha = blob_sha(data)
    if sha != body.get("sha"):
        raise ValueError("the file does not match its sha")
    return sha, parse(data)


async def refresh_pins(settings: Settings) -> None:
    """`refresh_all` on the store of `settings`. Never raises."""
    try:
        async with pool.connection(settings) as conn:
            await refresh_all(conn, Schemas.of(settings.data))
    except Exception as e:
        log.error("data.redaction_refresh_failed", why=type(e).__name__)


async def refresh_all(conn: AsyncConnection[DictRow], schemas: Schemas) -> None:
    """Fetch every pinned file: one at its approved sha becomes the set's rules; any other
    waits as pending. A failed fetch is noted on the set and changes nothing else."""
    cur = await conn.execute(
        f"select * from {schemas.data}.redaction_sets where repo is not null".encode()
    )
    for row in await cur.fetchall():
        try:
            await _refresh(conn, schemas, row)
        except Exception as e:  # one set's failure never stops the others
            log.error("data.redaction_refresh_failed", set=row["name"], why=type(e).__name__)


async def _refresh(conn: AsyncConnection[DictRow], schemas: Schemas, row: DictRow) -> None:
    d, tenant_id, name = schemas.data, row["tenant_id"], row["name"]
    key = (tenant_id, name)
    try:
        sha, rules = await _fetch(row)
    except (httpx.HTTPError, ValueError) as e:
        why = str(e) if isinstance(e, ValueError) else type(e).__name__
        await conn.execute(
            f"update {d}.redaction_sets set checked_at = now(), error = %s"
            " where tenant_id = %s and name = %s".encode(),
            (why[:300], *key),
        )
        return
    dumped = rules.model_dump(mode="json")
    async with conn.transaction():
        if sha == row["approved_sha"]:
            await conn.execute(
                f"update {d}.redaction_sets set rules = %s, pin_sha = %s, pending = null,"
                " checked_at = now(), error = null where tenant_id = %s and name = %s".encode(),
                (Jsonb(dumped), sha, *key),
            )
            if row["rules"] != dumped:
                await restart_sources(conn, schemas, tenant_id, name)
            return
        await conn.execute(
            f"update {d}.redaction_sets set pin_sha = %s, pending = %s, checked_at = now(),"
            " error = null where tenant_id = %s and name = %s".encode(),
            (sha, Jsonb(dumped), *key),
        )
        if row["pin_sha"] != sha:
            await audit.record_audit(
                conn, schemas, tenant_id=tenant_id, kind="redaction.pending", target=name,
                detail={"sha": sha},
            )  # fmt: skip


# ---- the admin API -------------------------------------------------------------------------


@dataclass(frozen=True)
class SetInfo:
    name: str
    repo: str | None
    path: str | None
    ref: str | None
    approved_sha: str | None
    pin_sha: str | None
    pending: bool
    """A changed file waits for approval; sources using the set are not syncing."""
    usable: bool
    checked_at: object
    error: str | None


@dataclass(frozen=True)
class SetDetail:
    info: SetInfo
    rules: Rules | None
    pending_rules: Rules | None


def _info(r: DictRow) -> SetInfo:
    pinned = r["repo"] is not None
    pending = pinned and r["pin_sha"] is not None and r["pin_sha"] != r["approved_sha"]
    return SetInfo(
        r["name"], r["repo"], r["path"], r["ref"], r["approved_sha"], r["pin_sha"], pending,
        r["rules"] is not None and not (pinned and r["pin_sha"] != r["approved_sha"]),
        r["checked_at"], r["error"],
    )  # fmt: skip


async def list_sets(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    *,
    by: uuid.UUID,
    admin: bool = False,
) -> list[SetInfo]:
    await identity.require_admin(conn, schemas, tenant_id, by, admin=admin)
    cur = await conn.execute(
        f"select * from {schemas.data}.redaction_sets where tenant_id = %s order by name".encode(),
        (tenant_id,),
    )
    return [_info(r) for r in await cur.fetchall()]


async def show_set(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    name: str,
    *,
    by: uuid.UUID,
    admin: bool = False,
) -> SetDetail:
    await identity.require_admin(conn, schemas, tenant_id, by, admin=admin)
    cur = await conn.execute(
        f"select * from {schemas.data}.redaction_sets where tenant_id = %s and name = %s".encode(),
        (tenant_id, name),
    )
    row = await cur.fetchone()
    if row is None:
        raise identity.Missing("No such redaction set.")
    return SetDetail(
        _info(row),
        None if row["rules"] is None else Rules.model_validate(row["rules"]),
        None if row["pending"] is None else Rules.model_validate(row["pending"]),
    )


async def approve(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    name: str,
    sha: str,
    *,
    by: uuid.UUID,
    admin: bool = False,
) -> SetInfo:
    """Approve the pending file `sha` of set `name`: its rules replace the approved ones and
    the sources using the set sync again from the start."""
    async with conn.transaction():
        await identity.require_admin(conn, schemas, tenant_id, by, admin=admin)
        cur = await conn.execute(
            f"update {schemas.data}.redaction_sets set rules = pending, approved_sha = pin_sha,"
            " pending = null, approved_by = %s, approved_at = now() where tenant_id = %s"
            " and name = %s and pin_sha = %s and pending is not null returning *".encode(),
            (by, tenant_id, name, sha),
        )
        row = await cur.fetchone()
        if row is None:
            raise identity.Conflict("That is not the file waiting for approval.")
        try:  # a file loaded before a check tightened
            Rules.model_validate(row["rules"])
        except ValidationError as e:
            raise identity.Conflict(f"That file can't be approved: {_where(e)}") from None
        await restart_sources(conn, schemas, tenant_id, name)
        await audit.record_audit(
            conn, schemas, tenant_id=tenant_id, kind="redaction.approve", account_id=by,
            target=name, detail={"sha": sha},
        )  # fmt: skip
    return _info(row)


async def set_redacted(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    redacted: bool,
    *,
    by: uuid.UUID,
    admin: bool = False,
) -> None:
    """Mark a person, by their account, as one whose reads always hide what a guest's would."""
    async with conn.transaction():
        await identity.require_admin(conn, schemas, tenant_id, by, admin=admin)
        await conn.execute(
            f"update {schemas.data}.identities set redacted = %s where tenant_id = %s"
            " and account_id = %s".encode(),
            (redacted, tenant_id, account_id),
        )
        await audit.record_audit(
            conn, schemas, tenant_id=tenant_id, kind="redaction.person", account_id=by,
            target=f"account:{account_id}", detail={"redacted": redacted},
        )  # fmt: skip

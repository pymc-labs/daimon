"""Driver-owned records; the host supplies transactional persistence.

No process-local storage is selected by the production factory. The scripted
fake supplies a memory transaction for offline probes. A host persists records
as rows (`rows_of`): each transaction names the rows it locks and loads
(`Keys`), sees them through a strict `view`, and writes back only `changes`.
Transactions on different sessions of one account never wait on each other.
"""

import copy
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, Protocol

from mux.contracts.events import Event
from mux.contracts.ids import ModelRef, Scope, SkillRef
from mux.contracts.receipts import DeletionReceipt, SendReceipt
from mux.contracts.resources import (
    Agent,
    Artifact,
    Environment,
    Session,
    SessionSpec,
    Skill,
    SkillUpload,
    SkillVersion,
)
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.skill_deployment import GCSBundleDeployment
from mux.drivers.gemini.transport import Object

Kind = Literal["account", "session", "agent", "environment", "skill", "artifact"]
RowKey = tuple[Kind, str]
ACCOUNT: RowKey = ("account", "")
KINDS: tuple[Kind, ...] = ("account", "session", "agent", "environment", "skill", "artifact")


@dataclass
class SessionRecord:
    owner: tuple[str, str]
    session: Session
    spec: SessionSpec
    model: ModelRef
    request: Object
    current: str | None = None
    environment_id: str | None = None
    root: str | None = None
    workspace_expires_at: datetime | None = None
    interactions: dict[str, str] = field(default_factory=lambda: {})
    events: dict[str, Event] = field(default_factory=lambda: {})
    observations: dict[str, UsageObservation] = field(default_factory=lambda: {})
    interaction_models: dict[str, ModelRef] = field(default_factory=dict[str, ModelRef])
    native_updated: dict[str, datetime] = field(default_factory=lambda: {})
    observation_history: dict[tuple[str, int], UsageObservation] = field(default_factory=lambda: {})
    unknown_delivery: bool = False
    # Native (Gemini-safe) mcp_server name -> declared connection name.
    mcp_server_names: dict[str, str] = field(default_factory=dict[str, str])
    # Interaction id -> SHA-256 of each storage-mount and MCP header sent for it. Lets a
    # later turn or a restart clear that interaction's echo; never the token.
    mount_echo_digests: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    # Uploaded skill ids still to place in the pre-created `environment_id`; while
    # any remain, no turn starts. `left_out`: session skill ids it goes without.
    uploading: tuple[str, ...] = ()
    left_out: tuple[str, ...] = ()
    # Send operation key -> (principal, request digest, receipt).
    sends: dict[str, tuple[str, str, SendReceipt]] = field(
        default_factory=dict[str, tuple[str, str, SendReceipt]]
    )
    # The rows a turn reads beside this one (`session_refs`), fixed at creation.
    refs: tuple[RowKey, ...] = ()
    # Retention dropped its history: only the binding's ContinuityLost remains.
    pruned: bool = False


@dataclass(frozen=True)
class UploadedBundle:
    """An uploaded skill version: its SKILL.md text and each other file's
    (path, size, sha256). The bytes stay with the host until upload time."""

    manifest: str
    files: tuple[tuple[str, int, str], ...]


@dataclass
class SkillRecord:
    owner: tuple[str, str]
    account_scope_id: str
    skill: Skill
    bundles: dict[str, SkillUpload] = field(default_factory=dict[str, SkillUpload])
    versions: dict[str, SkillVersion] = field(default_factory=dict[str, SkillVersion])
    deployments: dict[str, GCSBundleDeployment] = field(
        default_factory=dict[str, GCSBundleDeployment]
    )
    uploads: dict[str, UploadedBundle] = field(default_factory=dict[str, UploadedBundle])


@dataclass
class ArtifactRecord:
    artifact: Artifact
    digest: str
    body: bytes | None = None


@dataclass
class Records:
    agents: dict[str, Agent] = field(default_factory=lambda: {})
    environments: dict[str, Environment] = field(default_factory=lambda: {})
    sessions: dict[str, SessionRecord] = field(default_factory=lambda: {})
    skills: dict[str, SkillRecord] = field(default_factory=dict[str, SkillRecord])
    artifacts: dict[str, ArtifactRecord] = field(default_factory=dict[str, ArtifactRecord])
    artifact_pages: dict[str, tuple[str, str | None, str, tuple[str, ...]]] = field(
        default_factory=dict[str, tuple[str, str | None, str, tuple[str, ...]]]
    )
    # Workspace snapshot files the host reports as delivered, by artifact id ->
    # session id. Gemini cannot delete a file from a workspace, so this is the
    # delete: a listing leaves these out. The id covers the content digest, so a
    # rewritten file is listed again.
    delivered_artifacts: dict[str, str] = field(default_factory=dict[str, str])
    deletions: dict[tuple[str, str, str], tuple[str, str, DeletionReceipt]] = field(
        default_factory=dict[tuple[str, str, str], tuple[str, str, DeletionReceipt]]
    )
    # Legacy (one-blob) sends; `adopt_legacy` moves them into their sessions.
    sends: dict[tuple[str, str, str], tuple[str, str, SendReceipt]] = field(
        default_factory=lambda: {}
    )
    creations: dict[tuple[str, str, str], tuple[str, str, str]] = field(default_factory=lambda: {})


class RecordsScopeError(RuntimeError):
    """A transaction touched a row its `Keys` do not name: a driver bug, never data."""


@dataclass(frozen=True)
class Keys:
    """The rows one transaction may see.

    `lock`: rows it may write (locked). `read`: rows loaded unlocked. `refs`: also
    load, unlocked, each named session's `refs`. `scan`: every row of these kinds
    (cold paths). `create`: kinds that may gain rows. `everything`: every row,
    locked (tests and single-blob storage only).
    """

    lock: tuple[RowKey, ...] = ()
    read: tuple[RowKey, ...] = ()
    refs: bool = False
    scan: tuple[Kind, ...] = ()
    create: tuple[Kind, ...] = ()
    everything: bool = False


ALL = Keys(everything=True)


def session_key(id_: str) -> RowKey:
    return ("session", id_)


def skill_keys(pins: Iterable[SkillRef] | None) -> tuple[RowKey, ...]:
    return tuple(("skill", p.id) for p in pins or ())


def session_keys(id_: str, *, lock: bool = False, refs: bool = False) -> Keys:
    """One session's row, read or (`lock`) written, and with `refs` the rows it reads."""
    key = (session_key(id_),)
    return Keys(lock=key, refs=refs) if lock else Keys(read=key, refs=refs)


@dataclass
class AccountRow:
    creations: dict[tuple[str, str, str], tuple[str, str, str]] = field(
        default_factory=dict[tuple[str, str, str], tuple[str, str, str]]
    )
    deletions: dict[tuple[str, str, str], tuple[str, str, DeletionReceipt]] = field(
        default_factory=dict[tuple[str, str, str], tuple[str, str, DeletionReceipt]]
    )


@dataclass
class SessionRow:
    record: SessionRecord
    artifact_pages: dict[str, tuple[str, str | None, str, tuple[str, ...]]] = field(
        default_factory=dict[str, tuple[str, str | None, str, tuple[str, ...]]]
    )
    delivered_artifacts: dict[str, str] = field(default_factory=dict[str, str])


RowBody = AccountRow | SessionRow | Agent | Environment | SkillRecord | ArtifactRecord


class ScopedDict[K, V](dict[K, V]):
    """One kind's rows: naming an unloaded id, or listing an unscanned kind, raises."""

    def __init__(
        self, data: Mapping[K, V], *, kind: str, named: Iterable[K], scanned: bool, create: bool
    ) -> None:
        super().__init__(data)
        self._kind, self._named, self._scanned = kind, set(named), scanned
        self._create = create

    def _check(self, key: K) -> None:
        if not self._scanned and key not in self._named:
            raise RecordsScopeError(f"{self._kind} {key!r} is outside the transaction's keys")

    def _listing(self) -> None:
        if not self._scanned:
            raise RecordsScopeError(f"{self._kind} rows were not scanned")

    def __getitem__(self, key: K) -> V:
        self._check(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._check(key)  # pyright: ignore[reportArgumentType]  # dict accepts any key
        return super().__contains__(key)

    def __setitem__(self, key: K, value: V) -> None:
        if self._create:
            self._named.add(key)
        self._check(key)
        super().__setitem__(key, value)

    def __delitem__(self, key: K) -> None:
        self._check(key)
        super().__delitem__(key)

    def get(self, key: K, default: V | None = None, /) -> V | None:  # pyright: ignore[reportIncompatibleMethodOverride]  # rows are never None
        self._check(key)
        return super().get(key, default)

    def setdefault(self, key: K, default: V, /) -> V:  # pyright: ignore[reportIncompatibleMethodOverride]  # rows are never None
        if key not in self:
            self[key] = default
        return super().__getitem__(key)

    def raw(self) -> dict[K, V]:
        return dict(super().items())

    def __iter__(self) -> Iterator[K]:
        self._listing()
        return super().__iter__()

    def __len__(self) -> int:
        self._listing()
        return super().__len__()

    def keys(self):  # pyright: ignore[reportIncompatibleMethodOverride]  # same view type
        self._listing()
        return super().keys()

    def values(self):  # pyright: ignore[reportIncompatibleMethodOverride]  # same view type
        self._listing()
        return super().values()

    def items(self):  # pyright: ignore[reportIncompatibleMethodOverride]  # same view type
        self._listing()
        return super().items()


def _raw[K, V](rows: dict[K, V]) -> dict[K, V]:
    """The dict's contents past any scope check (views compare their raw rows)."""
    if isinstance(rows, ScopedDict):
        return rows.raw()
    return dict(rows)


def rows_of(records: Records) -> dict[RowKey, RowBody]:
    """One row per top-level record; pages and deliveries go with their session."""
    if _raw(records.sends):
        raise RecordsScopeError("legacy sends must be adopted into their sessions first")
    sessions = _raw(records.sessions)
    rows: dict[RowKey, RowBody] = {
        ACCOUNT: AccountRow(_raw(records.creations), _raw(records.deletions))
    }
    session_rows = {id_: SessionRow(record) for id_, record in sessions.items()}
    for digest, page in _raw(records.artifact_pages).items():
        if page[0] not in session_rows:
            raise RecordsScopeError(f"artifact page names unloaded session {page[0]!r}")
        session_rows[page[0]].artifact_pages[digest] = page
    for artifact, session in _raw(records.delivered_artifacts).items():
        if session not in session_rows:
            raise RecordsScopeError(f"delivered artifact names unloaded session {session!r}")
        session_rows[session].delivered_artifacts[artifact] = session
    rows.update({("session", id_): row for id_, row in session_rows.items()})
    rows.update({("agent", i): v for i, v in _raw(records.agents).items()})
    rows.update({("environment", i): v for i, v in _raw(records.environments).items()})
    rows.update({("skill", i): v for i, v in _raw(records.skills).items()})
    rows.update({("artifact", i): v for i, v in _raw(records.artifacts).items()})
    return rows


def session_refs(
    agent: Agent, environment: Environment | None, spec: SessionSpec
) -> tuple[RowKey, ...]:
    """The rows a session's turns read: its agent, environment, pinned skills and files."""
    refs: list[RowKey] = [("agent", agent.ref.id)]
    if environment is not None:
        refs.append(("environment", environment.ref.id))
        refs.extend(
            ("artifact", source.artifact.id)
            for source in environment.spec.sources or ()
            if source.artifact is not None
        )
    refs.extend(("skill", pin.id) for pin in (*(agent.spec.skills or ()), *spec.skills))
    return tuple(dict.fromkeys(refs))


def adopt_legacy(records: Records) -> None:
    """Fit a one-blob Records to rows: fill session refs, move sends into sessions.

    A send goes to the session whose interactions hold its first input id; one
    with no input ids is copied into every session with an unknown delivery (keys
    are per delivery, so the copies are never looked up). The rest are dropped:
    the StateStore operation answers those replays the same way.
    """
    for record in records.sessions.values():
        if not record.refs and (agent := records.agents.get(record.spec.agent.id)):
            env = record.spec.environment
            record.refs = session_refs(
                agent, records.environments.get(env.id) if env else None, record.spec
            )
    for (_, _, key), (principal, digest, receipt) in records.sends.items():
        for record in records.sessions.values():
            first = receipt.input_ids[0] if receipt.input_ids else None
            if (first is not None and first in record.interactions) or (
                first is None and record.unknown_delivery
            ):
                record.sends.setdefault(key, (principal, digest, receipt))
    records.sends.clear()


def records_of(rows: Mapping[RowKey, RowBody]) -> Records:
    """The inverse of `rows_of`: plain Records holding exactly these rows."""
    records = Records()
    for (kind, id_), body in rows.items():
        if kind == "account" and isinstance(body, AccountRow):
            records.creations.update(body.creations)
            records.deletions.update(body.deletions)
        elif kind == "session" and isinstance(body, SessionRow):
            records.sessions[id_] = body.record
            records.artifact_pages.update(body.artifact_pages)
            records.delivered_artifacts.update(body.delivered_artifacts)
        elif kind == "agent" and isinstance(body, Agent):
            records.agents[id_] = body
        elif kind == "environment" and isinstance(body, Environment):
            records.environments[id_] = body
        elif kind == "skill" and isinstance(body, SkillRecord):
            records.skills[id_] = body
        elif kind == "artifact" and isinstance(body, ArtifactRecord):
            records.artifacts[id_] = body
        else:
            raise ValueError(f"{kind} row holds {type(body).__name__}")
    return records


def scoped(rows: Mapping[RowKey, RowBody], keys: Keys) -> tuple[dict[RowKey, RowBody], Keys]:
    """For one-blob storage: the rows `keys` load, and keys with refs resolved."""
    if keys.everything:
        return dict(rows), keys
    read = [*keys.read]
    if keys.refs:
        for key in (*keys.lock, *keys.read):
            if isinstance(row := rows.get(key), SessionRow):
                read.extend(row.record.refs)
    resolved = replace(keys, read=tuple(read), refs=False)
    named = {*resolved.lock, *resolved.read}
    return {k: v for k, v in rows.items() if k in named or k[0] in keys.scan}, resolved


def view(rows: Mapping[RowKey, RowBody], keys: Keys, *, copy_rows: bool = True) -> Records:
    """Records over exactly `rows` (resolved `keys`): any other id raises on access.

    `copy_rows`: write transactions work on copies, so `changes` can compare with
    the rows as loaded. A pure read may share the rows (a decode cache) and must
    not mutate them.
    """
    loaded = copy.deepcopy(dict(rows)) if copy_rows else dict(rows)
    records = records_of(loaded)
    if keys.everything:
        return records
    named = {*keys.lock, *keys.read}
    account = ACCOUNT in named or "account" in keys.scan

    def scope[K, V](kind: Kind, values: dict[K, V], ids: Iterable[K]) -> dict[K, V]:
        return ScopedDict(
            values,
            kind=kind,
            named=ids,
            scanned=kind in keys.scan,
            create=kind in keys.create,
        )

    def ids(kind: Kind) -> set[str]:
        return {id_ for k, id_ in named if k == kind}

    records.sessions = scope("session", records.sessions, ids("session"))
    records.agents = scope("agent", records.agents, ids("agent"))
    records.environments = scope("environment", records.environments, ids("environment"))
    records.skills = scope("skill", records.skills, ids("skill"))
    records.artifacts = scope("artifact", records.artifacts, ids("artifact"))
    if not account:
        records.creations = ScopedDict(
            records.creations, kind="account", named=(), scanned=False, create=False
        )
        records.deletions = ScopedDict(
            records.deletions, kind="account", named=(), scanned=False, create=False
        )
    return records


@dataclass(frozen=True)
class Changes:
    upserts: dict[RowKey, RowBody]
    deletes: tuple[RowKey, ...]


def changes(records: Records, loaded: Mapping[RowKey, RowBody], keys: Keys) -> Changes:
    """The rows a transaction wrote; touching a row it only loaded raises."""
    current = rows_of(records)
    locked = set(keys.lock)
    upserts: dict[RowKey, RowBody] = {}
    for key, body in current.items():
        before = loaded.get(key)
        if key == ACCOUNT and before is None:
            before = AccountRow()  # never stored until it holds something
        if body == before:
            continue
        if keys.everything or key in locked or (before is None and key[0] in keys.create):
            upserts[key] = body
        else:
            raise RecordsScopeError(f"{key[0]} {key[1]!r} changed without a lock")
    deletes = tuple(key for key in loaded if key not in current)
    if not keys.everything and (unlocked := [k for k in deletes if k not in locked]):
        raise RecordsScopeError(f"{unlocked[0][0]} {unlocked[0][1]!r} deleted without a lock")
    return Changes(upserts, deletes)


class Storage(Protocol):
    def transaction(
        self, keys: Keys = ALL, *, after_dispatch: bool = False
    ) -> AbstractAsyncContextManager[Records]:
        """Serialize atomic writes to the rows `keys` lock; allow post-dispatch
        outcome acquisition to wait.

        The host's enclosing send timeout (120s by default, including pre-dispatch
        work and POST time) or earlier request cancellation still bounds that wait.
        """
        ...

    def read(self, keys: Keys = ALL) -> AbstractAsyncContextManager[Records]:
        """The last committed rows `keys` load, for a pure read: no lock, never
        saved, so listing usage or events never waits behind a writer. Mutating
        what it yields is a bug (the rows may be shared)."""
        ...


def owner(scope: Scope) -> tuple[str, str]:
    return scope.tenant_id, scope.account_id

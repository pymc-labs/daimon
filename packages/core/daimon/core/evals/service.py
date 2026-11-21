"""`EvalService`: what every surface (web, MCP, CLI) calls to work with evals.

It checks the `evals` feature and who may do what (`access`), and keeps every document as
the JSON the bundle format defines.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import tempfile
import time
import uuid
import zipfile
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import IO, Final, Literal

from anthropic import AsyncAnthropic
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from daimon.core.artifacts.s3 import build_artifact_store
from daimon.core.config import Settings
from daimon.core.evals import bundle as bundles
from daimon.core.evals import dedupe
from daimon.core.evals.access import (
    Action,
    Share,
    Subject,
    SuiteRole,
    allows,
    held_groups,
    suite_role,
)
from daimon.core.evals.agents import AgentLookup, agent_lookup
from daimon.core.evals.blobs import ObjectStore, blob_path, blob_refs, object_key
from daimon.core.evals.draft import (
    Complete,
    DraftError,
    DraftEstimate,
    draft_case,
    draft_model,
    estimate_draft,
)
from daimon.core.evals.format import (
    Arm,
    BlobEntry,
    Case,
    EventStream,
    ItemCaseRef,
    JsonObject,
    Manifest,
    Run,
    RunItem,
    RunOrigin,
    ScrubStatus,
    SourceInfo,
    Suite,
    SuiteDefaults,
    ToolAlias,
    canonical_json,
    case_hash,
    sha256_hex,
    to_document,
)
from daimon.core.evals.from_thread import ThreadNotFound, ThreadSource, build_cases
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.entities import (
    KnownEntities,
    PersonEntry,
    load_entities,
    merge_entities,
)
from daimon.core.evals.scrub.offline import MIN_KEY
from daimon.core.evals.validate import (
    MANIFEST_PATH,
    InvalidDocument,
    LoadedRun,
    LoadedSuite,
    ValidationReport,
    case_problems,
    case_warnings,
    schema_problems,
    validate_bundle,
)
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.stores import eval_scrub
from daimon.core.stores import evals as store
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CaseRef = ItemCaseRef
_SCRUB_ORDER: Final[tuple[ScrubStatus, ...]] = ("scrubbed", "partial", "unscrubbed")
OFF: Final = "Evals are off here. Ask an admin or an owner to turn them on."
_SPOOL_BYTES: Final = 8 * 1024 * 1024
_IMPORT_SECONDS: Final = 120.0
_UNFINISHED_RUN: Final = frozenset({"draft", "estimated", "approved", "running"})


class EvalRefused(Exception):
    """A request evals will not carry out; `problems` lists what to fix, when there is a list."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.problems = tuple(problems)


class EvalNotFound(EvalRefused):
    """No such thing, or one the actor may not see."""


class EvalForbidden(EvalRefused):
    """The actor may see it but not do this."""


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SuiteSummary(_Out):
    id: str
    name: str
    version: int
    role: SuiteRole
    scrub_status: ScrubStatus


class CaseSummary(_Out):
    id: str
    revision: int
    hash: str
    title: str
    fidelity: str


class SuiteCasePick(_Out):
    id: str
    revision: int | None = None
    """None: the case's latest revision."""
    weight: float = 1
    split: Literal["dev", "test", "holdout"] = "test"


class SuiteDraft(_Out):
    """A new suite or the next version of one. `cases` None keeps the cases it pins."""

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    name: str
    description: str | None = None
    tags: list[str] | None = None
    tool_aliases: list[ToolAlias] | None = None
    defaults: SuiteDefaults | None = None
    cases: list[SuiteCasePick] | None = None


class Page(_Out):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=500)


class RunItemSummary(_Out):
    id: str
    key: str
    case_id: str
    case_revision: int
    arm_id: str
    repeat: int
    status: str


class SuitePick(_Out):
    id: str
    version: int | None = None


class ExportRequest(_Out):
    suites: list[SuitePick] = []
    runs: list[str] = []
    title: str | None = None
    include_identities: bool = False
    """Admins only: keep who reviewed and owns instead of neutral labels."""


class CaseCheck(_Out):
    problems: list[str]
    warnings: list[str]


class ImportedSuite(_Out):
    bundle_id: str
    id: str
    version: int
    created: bool


class ImportReport(_Out):
    bundle_id: str
    suites: list[ImportedSuite]
    runs: list[str]
    skipped_runs: list[str]
    blobs: int
    signature: bundles.SignatureState
    warnings: list[str]


class DraftResult(_Out):
    estimate: DraftEstimate
    drafted: list[CaseRef]
    """Empty until the estimate is confirmed."""


class DedupeReport(_Out):
    groups: list[list[str]]
    dropped: list[str]


def _worst(statuses: Iterable[str | None]) -> ScrubStatus:
    """The least scrubbed of `statuses`; a missing or unknown one counts as unscrubbed."""
    found = [s if s in _SCRUB_ORDER else "unscrubbed" for s in statuses]
    return max(found, key=_SCRUB_ORDER.index, default="scrubbed")  # pyright: ignore[reportReturnType]


def _shares(rows: Iterable[store.ShareRow]) -> list[Share]:
    return [Share(kind=r.who_kind, id=r.who, role=r.role) for r in rows]  # pyright: ignore[reportArgumentType]


def _daimon_version() -> str | None:
    try:
        return version("daimon-core")
    except PackageNotFoundError:
        return None


def _keyed(secret: str, text: str) -> str:
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()


def _instance(secret: str) -> str:
    """An opaque, stable name for a workspace as a bundle source."""
    return _keyed(secret, "instance")[:16]


def _label_people(document: JsonObject, secret: str) -> JsonObject:
    """A copy with every person, group and agent reference replaced by a label, the same in
    every bundle this workspace exports and meaningless without its secret."""

    def walk(node: JsonValue) -> JsonValue:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        kind, ref = node.get("kind"), node.get("id")
        is_ref = node.keys() <= {"kind", "id", "display_name"} and isinstance(ref, str)
        if is_ref and kind in ("person", "group", "agent"):
            return {"kind": "label", "id": f"{kind}-{_keyed(secret, f'{kind}:{ref}')[:12]}"}
        return {k: walk(v) for k, v in node.items()}

    walked = walk(document)
    assert isinstance(walked, dict)
    return walked


def arm_labels(run: Run) -> dict[str, str]:
    """The neutral name of each arm a blind reviewer sees."""
    return run.blind_labels or {a.id: f"Arm {n}" for n, a in enumerate(run.arms, start=1)}


def blind_run(run: Run) -> Run:
    """A run as a blind reviewer sees it: arms under their neutral labels, and nothing that
    names a model, an agent, a cost or who approved it."""
    labels = arm_labels(run)
    arms = [Arm(id=n, label=n, kind=a.kind) for a in run.arms if (n := labels.get(a.id, "Arm"))]
    return Run(
        id=run.id, suite=run.suite, arms=arms, case_ids=run.case_ids, repeats=run.repeats,
        status=run.status, origin=run.origin, started_at=run.started_at,
        finished_at=run.finished_at,
    )  # fmt: skip


def blind_item(document: JsonObject, labels: dict[str, str]) -> JsonObject:
    """An item as a blind reviewer sees it: only what the agent said and did, under the arm's
    neutral label. Usage, the enclosure, extensions and the stored transcript are left out."""
    item = RunItem.model_validate(document)
    events = item.transcript.events if item.transcript else None
    quiet = [e.model_copy(update={"model": None, "usage": None}) for e in events or []]
    blind = RunItem(
        id=item.id, run_id=item.run_id, case=item.case, arm_id=labels.get(item.arm_id, "Arm"),
        repeat=item.repeat, status=item.status, error=item.error, started_at=item.started_at,
        finished_at=item.finished_at, transcript=EventStream(events=quiet) if events else None,
        chat=item.chat, outputs=item.outputs, world_calls=item.world_calls,
    )  # fmt: skip
    return to_document(blind)


@dataclass
class BundleFile:
    """An exported bundle, spooled to a temporary file; `chunks` streams and then closes it."""

    name: str
    media_type: str
    size: int
    sha256: str
    file: IO[bytes]

    async def chunks(self, size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        try:
            await asyncio.to_thread(self.file.seek, 0)
            while chunk := await asyncio.to_thread(self.file.read, size):
                yield chunk
        finally:
            self.file.close()

    async def read(self) -> bytes:
        return b"".join([c async for c in self.chunks()])


class EvalService:
    """`objects` keeps blob bytes (the artifact store); without one they stay in Postgres.
    `agents` checks the agents live arms name; without it such arms are refused."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        objects: ObjectStore | None = None,
        agents: AgentLookup | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._objects = objects
        self._agents = agents

    # ---------- checks ----------

    async def _check_on(self, actor: Subject) -> None:
        if actor.operator:
            return
        if actor.role == "visitor":
            raise EvalForbidden("Evals are for members. Ask an admin or an owner.")
        place = FeaturePlace(actor.tenant_id, actor.platform)
        snapshot = await resolve_features(self._sessions, self._settings, place, keys=("evals",))
        if not snapshot.on("evals"):
            raise EvalForbidden(OFF)

    async def _role(
        self, session: AsyncSession, actor: Subject, suite: store.SuiteRow, scrub: str | None
    ) -> SuiteRole | None:
        """The actor's role on a live suite, for content as scrubbed as `scrub`. Shares only
        reach scrubbed content: anything else is for owners and admins."""
        if suite.archived_at is not None:
            return None
        shares = _shares((await store.shares_of(session, [suite.id])).get(suite.id, []))
        groups = await held_groups(actor, shares)
        role = suite_role(actor, owner_key=suite.owner_key, shares=shares, groups=groups)
        return role if role == "owner" or scrub == "scrubbed" else None

    async def _suite(
        self, session: AsyncSession, actor: Subject, key: str, action: Action, *, lock: bool = False
    ) -> tuple[store.SuiteRow, SuiteRole]:
        suite = await store.suite_by_key(session, tenant_id=actor.tenant_id, key=key, lock=lock)
        role = suite and await self._role(session, actor, suite, suite.scrub_status)
        if suite is None or not role:
            raise EvalNotFound(f"No suite {key} here.")
        if not allows(role, action):
            raise EvalForbidden(f"You can't {action} this suite. Ask its owner.")
        return suite, role

    async def _version(
        self,
        session: AsyncSession,
        actor: Subject,
        key: str,
        version: int | None,
        action: Action = "view",
    ) -> tuple[store.SuiteRow, SuiteRole, int]:
        """A suite version the actor may act on; an older one is checked on its own."""
        suite, role = await self._suite(session, actor, key, action)
        number = version or suite.current_version
        scrub = await store.version_scrub(session, suite_id=suite.id, version=number)
        if scrub is None or (role != "owner" and scrub != "scrubbed"):
            raise EvalNotFound(f"Suite {key} has no version {number}.")
        return suite, role, number

    async def _run(
        self, session: AsyncSession, actor: Subject, run: store.RunRow | None, action: Action
    ) -> tuple[store.RunRow, SuiteRole]:
        suite = run and (await store.suites_by_id(session, [run.suite_id])).get(run.suite_id)
        role = run and suite and await self._role(session, actor, suite, run.scrub_status)
        if run is None or not role:
            raise EvalNotFound("No such run here.")
        if not allows(role, action):
            raise EvalForbidden(f"You can't {action} this run. Ask its suite's owner.")
        return run, role

    # ---------- blob bytes ----------

    async def _put_blob(
        self, session: AsyncSession, tenant_id: uuid.UUID, row: store.BlobRow, content: bytes
    ) -> None:
        if self._objects is not None:
            key = object_key(tenant_id, row.sha256)
            await self._objects.put(key=key, content=content, content_type=row.media_type)
        kept = None if self._objects is not None else content
        await store.put_blob(session, tenant_id=tenant_id, blob=row, content=kept)

    async def _read_blob(
        self, session: AsyncSession, tenant_id: uuid.UUID, sha256: str
    ) -> bytes | None:
        content = await store.blob_content(session, tenant_id=tenant_id, sha256=sha256)
        if content is None and self._objects is not None:
            content = await self._objects.read(key=object_key(tenant_id, sha256))
        return content

    # ---------- suites and cases ----------

    async def suites(self, actor: Subject) -> list[SuiteSummary]:
        await self._check_on(actor)
        async with self._sessions() as session:
            rows = await store.list_suites(session, tenant_id=actor.tenant_id)
            shares = await store.shares_of(session, [r.id for r in rows])
            groups = await held_groups(actor, _shares(s for rs in shares.values() for s in rs))
        found: list[SuiteSummary] = []
        for row in rows:
            role = suite_role(
                actor,
                owner_key=row.owner_key,
                shares=_shares(shares.get(row.id, [])),
                groups=groups,
            )
            if role == "owner" or (role is not None and row.scrub_status == "scrubbed"):
                found.append(
                    SuiteSummary(
                        id=row.key, name=row.name, version=row.current_version, role=role,
                        scrub_status=_worst([row.scrub_status]),
                    )
                )  # fmt: skip
        return found

    async def suite(self, actor: Subject, suite_id: str, version: int | None = None) -> Suite:
        await self._check_on(actor)
        async with self._sessions() as session:
            suite, _, number = await self._version(session, actor, suite_id, version)
            document = await store.suite_document(session, suite_id=suite.id, version=number)
        assert document is not None
        return Suite.model_validate(document)

    async def cases(
        self, actor: Subject, suite_id: str, version: int | None = None
    ) -> list[CaseSummary]:
        await self._check_on(actor)
        async with self._sessions() as session:
            suite, _, number = await self._version(session, actor, suite_id, version)
            rows = await store.version_cases(session, suite_id=suite.id, version=number)
        return [
            CaseSummary(
                id=r.key, revision=r.revision, hash=r.sha256, title=case.title,
                fidelity=case.fidelity.level,
            )
            for r in rows
            for case in [_case_model(r.document)]
        ]  # fmt: skip

    async def case(
        self, actor: Subject, suite_id: str, case_id: str, revision: int | None = None
    ) -> JsonObject:
        await self._check_on(actor)
        async with self._sessions() as session:
            suite, role = await self._suite(session, actor, suite_id, "view")
            row = await store.case_revision(
                session, suite_id=suite.id, key=case_id, revision=revision
            )
        if row is None or (role != "owner" and row.scrub_status != "scrubbed"):
            raise EvalNotFound(f"No case {case_id} in suite {suite_id}.")
        return row.document

    @staticmethod
    def validate_case(document: JsonObject) -> CaseCheck:
        """Problems and warnings of a case, without storing it; a missing hash is fine."""
        problems = case_problems(document, need_hash=False)
        warnings = [] if problems else case_warnings(_case_model(document), "case")
        return CaseCheck(problems=problems, warnings=warnings)

    async def check_case(self, actor: Subject, document: JsonObject) -> CaseCheck:
        """`validate_case` for someone who may use evals here."""
        await self._check_on(actor)
        return self.validate_case(document)

    async def save_suite(
        self, actor: Subject, draft: SuiteDraft, *, new: bool | None = None
    ) -> Suite:
        """Create the suite or add its next version; `new` True or False insists on one."""
        await self._check_on(actor)
        async with self._sessions() as session, session.begin():
            existing = await store.suite_by_key(
                session, tenant_id=actor.tenant_id, key=draft.id, lock=True
            )
            if new and existing is not None:
                raise EvalRefused(f"A suite {draft.id} already exists. Pick another id.")
            if new is False and existing is None:
                raise EvalNotFound(f"No suite {draft.id} here.")
            if existing is None:
                if draft.cases:
                    raise EvalRefused("A new suite starts with no cases. Add them, then pin them.")
                suite = await store.create_suite(
                    session,
                    tenant_id=actor.tenant_id,
                    key=draft.id,
                    name=draft.name,
                    owner_key=actor.key,
                )
                if suite is None:
                    raise EvalRefused(f"A suite {draft.id} already exists. Pick another id.")
                previous: JsonObject = {}
            else:
                suite, _ = await self._suite(session, actor, draft.id, "edit")
                previous = (
                    await store.suite_document(
                        session, suite_id=suite.id, version=suite.current_version
                    )
                    or {}
                )
            refs = _pins_of(previous)
            if draft.cases is not None:
                refs = await self._picked(session, suite, draft.cases)
            document = to_document(
                Suite(
                    id=draft.id, version=suite.current_version + 1, name=draft.name,
                    description=draft.description, tags=draft.tags, created_at=datetime.now(UTC),
                    cases=refs, tool_aliases=draft.tool_aliases, defaults=draft.defaults,  # pyright: ignore[reportArgumentType]
                )
            )  # fmt: skip
            await self._store_version(session, suite, document, actor)
        return Suite.model_validate(document)

    async def _picked(
        self, session: AsyncSession, suite: store.SuiteRow, picks: Sequence[SuiteCasePick]
    ) -> list[JsonObject]:
        picked: list[JsonObject] = []
        for pick in picks:
            found = await store.case_revision(
                session, suite_id=suite.id, key=pick.id, revision=pick.revision
            )
            if found is None:
                raise EvalNotFound(f"No case {pick.id} in suite {suite.key}.")
            picked.append(
                {
                    "id": pick.id, "revision": found.revision, "hash": found.sha256,
                    "weight": pick.weight, "split": pick.split,
                }
            )  # fmt: skip
        return picked

    async def _store_version(
        self,
        session: AsyncSession,
        suite: store.SuiteRow,
        document: JsonObject,
        actor: Subject,
        *,
        signature: str | None = None,
        bundle_scrub: str | None = None,
    ) -> None:
        """Store a version; how scrubbed it is comes from the stored revisions it pins, and
        from the bundle it came in, when it did."""
        if problems := schema_problems("Suite", document, "suite"):
            raise EvalRefused("This suite is not valid.", problems)
        ids = await store.case_ids(session, suite_id=suite.id)
        refs = _pins_of(document)
        if missing := sorted({str(r["id"]) for r in refs} - ids.keys()):
            raise EvalNotFound("This suite pins cases it does not have.", missing)
        pins = [
            store.PinRow(
                case_id=ids[str(r["id"])], revision=int(str(r["revision"])),
                weight=float(str(r.get("weight", 1))), split=str(r.get("split", "test")),
            )
            for r in refs
        ]  # fmt: skip
        scrubs = await store.revision_scrubs(session, [(p.case_id, p.revision) for p in pins])
        await store.add_suite_version(
            session, suite, version=int(str(document["version"])), document=document, pins=pins,
            scrub_status=_worst([*scrubs, bundle_scrub]) if bundle_scrub else _worst(scrubs),
            created_by=actor.key, signature=signature,
        )  # fmt: skip

    async def put_case(
        self,
        actor: Subject,
        suite_id: str,
        case: JsonObject,
        *,
        blobs: Mapping[BlobEntry, bytes] | None = None,
    ) -> CaseRef:
        """Store `case` as the next revision of its id; `save_suite` pins it. Files it uses
        must already be in the suite, or come in `blobs`."""
        await self._check_on(actor)
        key = str(case.get("id"))
        async with self._sessions() as session, session.begin():
            suite, _ = await self._suite(session, actor, suite_id, "edit", lock=True)
            latest = await store.case_revision(session, suite_id=suite.id, key=key)
            document = {k: v for k, v in case.items() if k != "hash"}
            document["revision"] = latest.revision + 1 if latest else 1
            document["hash"] = case_hash(document)
            if problems := case_problems(document):
                raise EvalRefused("This case is not valid.", problems)
            new = dict(blobs or {})
            if bad := [e.sha256 for e, data in new.items() if not _matches(e, data)]:
                raise EvalRefused("These files do not match their names or sizes.", bad)
            shas = blob_refs(document)
            have = await store.suite_blobs(session, suite_id=suite.id)
            if missing := sorted(shas - have - {e.sha256 for e in new}):
                raise EvalRefused("This case uses files the suite does not have.", missing)
            for entry, data in new.items():
                row = store.BlobRow.model_validate(entry.model_dump())
                await self._put_blob(session, actor.tenant_id, row, data)
            model = _case_model(document)
            await store.add_case_revision(
                session, suite=suite, key=key, revision=model.revision, document=document,
                sha256=str(document["hash"]), fidelity=model.fidelity.level,
                flags=[f.reason for f in model.review_flags or []], blob_shas=shas,
                scrub_status=await _with_blobs(session, actor.tenant_id, model, shas),
                created_by=actor.key,
            )  # fmt: skip
        return CaseRef(id=key, revision=model.revision, hash=str(document["hash"]))

    # ---------- cases from threads ----------

    async def _scrubber(self, tenant_id: uuid.UUID, known: KnownEntities) -> Scrubber:
        settings = self._settings.evals
        key = settings.scrub_key.get_secret_value() if settings.scrub_key else ""
        if len(key) < MIN_KEY:
            raise EvalRefused(
                f"Scrubbing needs DAIMON_EVALS__SCRUB_KEY, at least {MIN_KEY} characters."
            )
        if importlib.util.find_spec("faker") is None:
            raise EvalRefused(
                "Scrubbing needs the evals extra installed here. Ask an admin or an owner."
            )
        file = (
            [load_entities(Path(settings.scrub_entities_file))]
            if settings.scrub_entities_file
            else []
        )
        async with self._sessions() as session:
            fakes = await eval_scrub.load_map(session, tenant_id=tenant_id)
            names = await eval_scrub.deployment_names(session, tenant_id=tenant_id)
        return Scrubber(
            merge_entities(known, _deployment_entities(names), *file), key.encode(),
            salt=str(tenant_id), ner_model=settings.scrub_ner_model or None, known=fakes,
        )  # fmt: skip

    async def _pin(
        self, actor: Subject, suite_id: str, refs: Sequence[CaseRef], drop: Iterable[str] = ()
    ) -> None:
        """Save the suite's next version pinning `refs` (added or moved to that revision)."""
        current = await self.suite(actor, suite_id)
        moved = {r.id: r.revision for r in refs}
        gone = set(drop)
        picks = [
            SuiteCasePick(
                id=c.id, revision=moved.pop(c.id, c.revision), weight=c.weight, split=c.split
            )
            for c in current.cases
            if c.id not in gone
        ]
        picks += [SuiteCasePick(id=key, revision=rev) for key, rev in moved.items()]
        await self.save_suite(
            actor,
            SuiteDraft(
                id=current.id, name=current.name, description=current.description,
                tags=current.tags, tool_aliases=current.tool_aliases, defaults=current.defaults,
                cases=picks,
            ),
            new=False,
        )  # fmt: skip

    async def cases_from_thread(
        self, actor: Subject, source: ThreadSource, ref: str, suite_id: str
    ) -> list[CaseRef]:
        """Scrubbed cases, one per answered turn of the thread, added to the suite."""
        await self._check_on(actor)
        async with self._sessions() as session:
            await self._suite(session, actor, suite_id, "edit")
        try:
            record = await source.thread(ref)
        except ThreadNotFound:
            raise EvalNotFound("No such thread here, or you can't read it.") from None
        scrubber = await self._scrubber(actor.tenant_id, record.entities())
        built = await asyncio.to_thread(build_cases, record, scrubber)
        if not built:
            raise EvalRefused("That thread has no turn the agent answered.")
        async with self._sessions() as session, session.begin():
            await eval_scrub.save_map(session, tenant_id=actor.tenant_id, rows=scrubber.fakes.added)
        refs = [
            await self.put_case(
                actor, suite_id, item.case,
                blobs={
                    BlobEntry(
                        sha256=sha, size=len(b.data), media_type=b.media_type,
                        scrub=b.scrub, needs_human_check=b.needs_human_check,  # pyright: ignore[reportArgumentType]
                    ): b.data
                    for sha, b in item.blobs.items()
                },
            )
            for item in built
        ]  # fmt: skip
        await self._pin(actor, suite_id, refs)
        return refs

    async def draft_expectations(
        self,
        actor: Subject,
        suite_id: str,
        case_ids: Sequence[str],
        *,
        complete: Complete | None,
        confirm: bool,
    ) -> DraftResult:
        """Draft expectations for cases with a model. Without `confirm` only the cost estimate."""
        await self._check_on(actor)
        async with self._sessions() as session:
            await self._suite(session, actor, suite_id, "edit")  # before any paid call
        documents = [await self.case(actor, suite_id, key) for key in case_ids]
        for document in documents:
            scrub = _case_model(document).provenance.scrub
            if scrub is None or scrub.status != "scrubbed":
                raise EvalRefused("Drafting sends a case to a model: only scrubbed cases can go.")
        try:
            estimate = estimate_draft(documents, draft_model(self._settings.evals.draft_model))
            if not confirm:
                return DraftResult(estimate=estimate, drafted=[])
            if complete is None:
                raise EvalRefused("No model is available to draft with here.")
            refs: list[CaseRef] = []
            for document in documents:
                drafted, _ = await draft_case(complete, document)
                refs.append(await self.put_case(actor, suite_id, drafted))
        except DraftError as exc:
            raise EvalRefused(str(exc)) from None
        await self._pin(actor, suite_id, refs)
        return DraftResult(estimate=estimate, drafted=refs)

    async def dedupe(
        self,
        actor: Subject,
        suite_id: str,
        *,
        threshold: float = dedupe.THRESHOLD,
        keep: int | None = None,
    ) -> DedupeReport:
        """Mark near-duplicate cases with a `dedupe_group`; with `keep`, drop all but that many
        of each group from the suite's next version."""
        await self._check_on(actor)
        async with self._sessions() as session:
            suite, _ = await self._suite(session, actor, suite_id, "edit")
            rows = await store.version_cases(
                session, suite_id=suite.id, version=suite.current_version
            )
        found = await asyncio.to_thread(dedupe.groups, [r.document for r in rows], threshold)
        by_key = {r.key: r.document for r in rows}
        marked = [
            await self.put_case(actor, suite_id, {**by_key[key], "dedupe_group": name})
            for ids in found
            for name in [dedupe.group_id(ids)]
            for key in ids
            if by_key[key].get("dedupe_group") != name
        ]
        dropped = dedupe.thin(found, keep) if keep is not None else []
        if marked or dropped:
            await self._pin(actor, suite_id, marked, dropped)  # one new version for all of it
        return DedupeReport(groups=found, dropped=dropped)

    async def share(self, actor: Subject, suite_id: str, shares: Sequence[Share]) -> None:
        """Replace who the suite is shared with."""
        await self._check_on(actor)
        if len({(s.kind, s.id) for s in shares}) != len(shares):
            raise EvalRefused("Each person or group can be shared with once.")
        async with self._sessions() as session, session.begin():
            suite, _ = await self._suite(session, actor, suite_id, "share", lock=True)
            if shares and suite.scrub_status != "scrubbed":
                raise EvalForbidden(
                    "This suite is not scrubbed, so only owners and admins can see it."
                )
            rows = [store.ShareRow(who_kind=s.kind, who=s.id, role=s.role) for s in shares]
            await store.set_shares(session, suite, rows)

    # ---------- arms, runs and items ----------

    async def arms(self, actor: Subject) -> list[Arm]:
        await self._check_on(actor)
        async with self._sessions() as session:
            rows = await store.list_arms(session, tenant_id=actor.tenant_id)
        return [Arm.model_validate(r.document) for r in rows]

    async def save_arm(self, actor: Subject, arm: Arm) -> Arm:
        """Create an arm, or replace one its creator or an admin owns. A live arm's agent must
        be one the workspace has; `config_hash` is computed here from it and the arm."""
        await self._check_on(actor)
        config_hash = None
        if arm.kind == "live" and arm.live is not None:
            config: JsonValue = None
            if arm.live.agent is not None:
                if self._agents is None:
                    raise EvalRefused("Agents can't be checked here. Ask an admin or an owner.")
                config = await self._agents(actor.tenant_id, arm.live.agent)
                if config is None:
                    raise EvalNotFound(f"No agent {arm.live.agent} here.")
            live = to_document(arm.live)
            config_hash = sha256_hex(canonical_json({"agent": config, "live": live}))
        document = to_document(arm.model_copy(update={"config_hash": config_hash}))
        if problems := schema_problems("Arm", document, "arm"):  # pyright: ignore[reportArgumentType]
            raise EvalRefused("This arm is not valid.", problems)
        async with self._sessions() as session, session.begin():
            existing = await store.arm_by_key(session, tenant_id=actor.tenant_id, key=arm.id)
            if existing and not (actor.is_admin or existing.created_by in actor.ids):
                raise EvalForbidden("Someone else made this arm. Ask an admin or an owner.")
            row = await store.save_arm(
                session, tenant_id=actor.tenant_id, document=document, created_by=actor.key
            )
        return Arm.model_validate(row.document)

    async def run(self, actor: Subject, run_id: str, *, blind: bool = False) -> Run:
        """A run. Reviewers, and `blind`, get the blind view (`blind_run`)."""
        await self._check_on(actor)
        async with self._sessions() as session:
            found = await store.run_by_key(session, tenant_id=actor.tenant_id, key=run_id)
            run, role = await self._run(session, actor, found, "view")
        document = Run.model_validate(run.document)
        return blind_run(document) if blind or role == "reviewer" else document

    async def items(
        self, actor: Subject, run_id: str, page: Page, *, blind: bool = False
    ) -> list[RunItemSummary]:
        """A page of a run's items; reviewers, and `blind`, see arms by their neutral label."""
        await self._check_on(actor)
        async with self._sessions() as session:
            found = await store.run_by_key(session, tenant_id=actor.tenant_id, key=run_id)
            run, role = await self._run(session, actor, found, "view")
            rows = await store.items_of_run(
                session, run_id=run.id, offset=page.offset, limit=page.limit
            )
        labels = arm_labels(Run.model_validate(run.document)) if blind or role == "reviewer" else {}
        return [
            RunItemSummary(
                id=str(r.id), key=str(r.id) if labels else r.key, case_id=r.case_key,
                case_revision=r.case_revision,
                arm_id=labels.get(r.arm_key, "Arm") if labels else r.arm_key, repeat=r.repeat,
                status=r.status,
            )
            for r in rows
        ]  # fmt: skip

    async def item(self, actor: Subject, item_id: str, *, blind: bool = False) -> RunItem:
        """One run item. Reviewers, and `blind`, get the blind view (`blind_item`)."""
        await self._check_on(actor)
        try:
            item_uuid = uuid.UUID(item_id)
        except ValueError:
            raise EvalNotFound("No such item here.") from None
        async with self._sessions() as session:
            row = await store.item_by_id(session, tenant_id=actor.tenant_id, item_id=item_uuid)
            if row is None:
                raise EvalNotFound("No such item here.")
            run, role = await self._run(
                session, actor, await store.run_by_id(session, row.run_id), "view"
            )
        document = row.document
        if blind or role == "reviewer":
            document = blind_item(document, arm_labels(Run.model_validate(run.document)))
            document = {**document, "id": str(row.id)}  # the stored id can name the arm
        return RunItem.model_validate(document)

    async def blob(self, actor: Subject, sha256: str) -> tuple[store.BlobRow, bytes]:
        """A blob the actor may read: one a live suite's cases or runs reference, at content
        the actor may see there."""
        await self._check_on(actor)
        async with self._sessions() as session:
            rows = await store.blob_rows(session, tenant_id=actor.tenant_id, shas=[sha256])
            readable = actor.is_admin and sha256 in rows
            if sha256 in rows and not readable:
                uses = await store.blob_uses(session, tenant_id=actor.tenant_id, sha256=sha256)
                suites = await store.suites_by_id(session, {s for s, _ in uses})
                for suite_id, scrub in uses:
                    if await self._role(session, actor, suites[suite_id], scrub):
                        readable = True
                        break
            content = await self._read_blob(session, actor.tenant_id, sha256) if readable else None
        if content is None:
            raise EvalNotFound("No such file here.")
        return rows[sha256], content

    # ---------- bundles ----------

    async def export_bundle(
        self, actor: Subject, request: ExportRequest, *, passphrase: str | None = None
    ) -> BundleFile:
        """Write the chosen suites and runs as a bundle, encrypted with `passphrase` if given.
        Nothing is kept but the record that it was exported."""
        await self._check_on(actor)
        if request.include_identities and not actor.is_admin:
            raise EvalForbidden("Only an admin or an owner can export who did what.")
        suites: list[LoadedSuite] = []
        runs: list[LoadedRun] = []
        statuses: list[str | None] = []
        roles: list[SuiteRole] = []
        pinned: list[JsonObject] = []
        async with self._sessions() as session, session.begin():
            secret = await store.instance_secret(session, tenant_id=actor.tenant_id)
            for pick in request.suites:
                suite, role, number = await self._version(
                    session, actor, pick.id, pick.version, "export"
                )
                document = await store.suite_document(session, suite_id=suite.id, version=number)
                assert document is not None
                cases = [c.document for c in await store.version_cases(
                    session, suite_id=suite.id, version=number
                )]  # fmt: skip
                statuses.append(
                    await store.version_scrub(session, suite_id=suite.id, version=number)
                )
                roles.append(role)
                suites.append(LoadedSuite(document, cases))
            for key in request.runs:
                found = await store.run_by_key(session, tenant_id=actor.tenant_id, key=key)
                run, role = await self._run(session, actor, found, "export")
                statuses.append(run.scrub_status)
                roles.append(role)
                pinned += [c.document for c in await store.version_cases(
                    session, suite_id=run.suite_id, version=run.suite_version
                )]  # fmt: skip
                items, scores, reviews = await store.run_parts(session, run_id=run.id)
                runs.append(LoadedRun(run.document, items, scores, reviews))
            if not suites and not runs:
                raise EvalRefused("Pick at least one suite or run to export.")
            documents: list[JsonValue] = [d for s in suites for d in (s.document, *s.cases)]
            documents += [d for r in runs for d in (r.document, *r.items, *r.scores, *r.reviews)]
            blob_rows = await store.blob_rows(
                session, tenant_id=actor.tenant_id, shas=blob_refs(documents)
            )
            if missing := sorted(blob_refs(documents) - blob_rows.keys()):
                raise EvalRefused("Some files these documents use are missing.", missing)
            status = _worst(
                [*statuses, *("unscrubbed" for b in blob_rows.values() if b.scrub == "unscrubbed")]
            )
            if status != "scrubbed" and not (actor.is_admin or all(r == "owner" for r in roles)):
                raise EvalForbidden("Only the owner or an admin can export what is not scrubbed.")
            if not request.include_identities:
                suites, runs = _labelled(suites, runs, pinned, secret)
            entries = [
                BlobEntry(
                    sha256=sha, size=row.size, media_type=row.media_type, scrub=row.scrub,  # pyright: ignore[reportArgumentType]
                    needs_human_check=row.needs_human_check,
                )
                for sha, row in sorted(blob_rows.items())
            ]  # fmt: skip
            key = self._settings.evals.signing_key
            header = bundles.BundleHeader(
                id=f"bundle-{uuid.uuid4().hex[:16]}",
                created_at=datetime.now(UTC).replace(microsecond=0),
                title=request.title,
                source=SourceInfo(
                    kind="deployment",
                    label=self._settings.evals.source_label,
                    instance=_instance(secret),
                    daimon_version=_daimon_version(),
                ),  # fmt: skip
                scrub_status=status,
                includes_identities=request.include_identities,
            )
            files = bundles.document_files(
                header, suites, runs, entries,
                signing_key=bundles.private_key(key.get_secret_value()) if key else None,
            )  # fmt: skip
            await store.record_bundle(
                session, tenant_id=actor.tenant_id, direction="export",
                manifest=json.loads(files[MANIFEST_PATH]), actor_key=actor.key,
                signature="valid" if key else "unsigned",
            )  # fmt: skip
            spool = await self._zip(session, actor.tenant_id, files, entries)
            limit = self._settings.evals.max_bundle_mb
            if await asyncio.to_thread(_size, spool) > limit * 1024 * 1024:
                spool.close()
                raise EvalRefused(f"The bundle is over {limit} MiB. Export fewer suites or runs.")
        name, media_type = f"{header.id}.daimon-eval.zip", "application/zip"
        if passphrase:
            sealed = await asyncio.to_thread(bundles.encrypt, spool.read(), passphrase)
            spool.close()
            spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115
            spool.write(sealed)
            name, media_type = f"{header.id}.daimon-eval.enc", "application/octet-stream"
        size, sha = await asyncio.to_thread(_measure, spool)
        return BundleFile(name=name, media_type=media_type, size=size, sha256=sha, file=spool)

    async def _zip(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        files: Mapping[str, bytes],
        entries: Sequence[BlobEntry],
    ) -> IO[bytes]:
        """The bundle zip in a temporary file, blobs read one at a time; rewound."""
        spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115
        try:
            with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(files, key=lambda p: (p != MANIFEST_PATH, p)):
                    await asyncio.to_thread(bundles.zip_add, archive, path, files[path])
                for entry in entries:
                    data = await self._read_blob(session, tenant_id, entry.sha256)
                    if data is None:
                        raise EvalRefused(
                            "Some files these documents use are missing.", [entry.sha256]
                        )
                    await asyncio.to_thread(bundles.zip_add, archive, blob_path(entry.sha256), data)
        except BaseException:
            spool.close()
            raise
        spool.seek(0)
        return spool

    async def import_bundle(
        self,
        actor: Subject,
        data: AsyncIterable[bytes],
        *,
        accept_unscrubbed: bool = False,
        passphrase: str | None = None,
    ) -> ImportReport:
        """Validate a bundle, then store its suites, runs and blobs. The importer owns them."""
        await self._check_on(actor)
        if accept_unscrubbed and not actor.is_admin:
            raise EvalForbidden(
                "Only an owner or an admin can accept a bundle that is not scrubbed."
            )
        limit = self._settings.evals.max_bundle_mb * 1024 * 1024
        with tempfile.TemporaryFile() as spool:
            size = 0
            async for chunk in data:
                size += len(chunk)
                if size > limit:
                    raise EvalRefused(
                        f"The bundle is over {self._settings.evals.max_bundle_mb} MiB."
                    )
                await asyncio.to_thread(spool.write, chunk)
            await asyncio.to_thread(spool.seek, 0)
            try:
                source, report, raw_manifest, signature = await asyncio.to_thread(
                    self._open, spool, passphrase, limit
                )
            except InvalidDocument as invalid:
                raise EvalRefused("This bundle is not valid.", invalid.problems) from None
            manifest = report.manifest
            assert manifest is not None
            if self._settings.evals.trusted_keys and signature != "valid":
                raise EvalForbidden("Only bundles signed by a trusted key import here.")
            if refusal := bundles.scrub_refusal(manifest, accept_unscrubbed=accept_unscrubbed):
                raise EvalForbidden(refusal)
            return await self._store_bundle(actor, source, report, raw_manifest, signature)

    def _open(
        self, spool: IO[bytes], passphrase: str | None, limit: int
    ) -> tuple[bundles.ZipSource, ValidationReport, JsonObject, bundles.SignatureState]:
        """Decrypt, open, validate and verify, within `_IMPORT_SECONDS`. Runs in a thread."""
        source = bundles.open_bundle(
            spool, passphrase=passphrase, max_bytes=limit,
            deadline=time.monotonic() + _IMPORT_SECONDS,
        )  # fmt: skip
        report = validate_bundle(source)
        report.raise_for_problems()
        raw_manifest = json.loads(source.read(MANIFEST_PATH))
        signature = bundles.verify(raw_manifest, self._trusted_keys())
        source.deadline = None
        return source, report, raw_manifest, signature

    async def _store_bundle(
        self,
        actor: Subject,
        source: bundles.ZipSource,
        report: ValidationReport,
        raw_manifest: JsonObject,
        signature: bundles.SignatureState,
    ) -> ImportReport:
        manifest = report.manifest
        assert manifest is not None
        async with self._sessions() as session, session.begin():
            own = _instance(await store.instance_secret(session, tenant_id=actor.tenant_id))
            for entry in manifest.blobs:
                row = store.BlobRow.model_validate(entry.model_dump())
                content = await asyncio.to_thread(source.read, blob_path(entry.sha256))
                await self._put_blob(session, actor.tenant_id, row, content)
            imported = [
                await self._import_suite(session, actor, manifest, s, own, signature)
                for s in report.suites
            ]
            renamed = {s.bundle_id: s.id for s in imported}
            done: list[str] = []
            skipped: list[str] = []
            for run in report.runs:
                run_id = str(run.document["id"])
                if await store.run_by_key(session, tenant_id=actor.tenant_id, key=run_id):
                    skipped.append(run_id)
                    continue
                try:  # a concurrent import of the same run wins; this one skips it
                    async with session.begin_nested():
                        await self._import_run(session, actor, manifest, run, renamed, signature)
                except IntegrityError:
                    skipped.append(run_id)
                    continue
                done.append(run_id)
            await store.record_bundle(
                session, tenant_id=actor.tenant_id, direction="import", manifest=raw_manifest,
                actor_key=actor.key, signature=signature,
            )  # fmt: skip
        return ImportReport(
            bundle_id=manifest.id, suites=imported, runs=done, skipped_runs=skipped,
            blobs=len(manifest.blobs), signature=signature, warnings=report.warnings,
        )  # fmt: skip

    def _trusted_keys(self) -> list[Ed25519PublicKey]:
        keys = [bundles.public_key(k) for k in self._settings.evals.trusted_keys]
        own = self._settings.evals.signing_key
        if own is not None:
            keys.append(bundles.private_key(own.get_secret_value()).public_key())
        return keys

    async def _import_suite(
        self,
        session: AsyncSession,
        actor: Subject,
        manifest: Manifest,
        loaded: LoadedSuite,
        own: str,
        signature: str,
    ) -> ImportedSuite:
        """A suite lands as a new version of the same suite from the same source when the actor
        may edit it and no case revision conflicts; otherwise as a new suite."""
        key, number = str(loaded.document["id"]), int(str(loaded.document["version"]))
        instance = manifest.source.instance
        existing = await store.suite_by_key(session, tenant_id=actor.tenant_id, key=key, lock=True)
        if existing is not None:
            same_source = (existing.source_instance or own) == instance
            hashes = await store.case_hashes(session, suite_id=existing.id)
            clash = any(
                hashes.get((str(c["id"]), int(str(c["revision"]))), c["hash"]) != c["hash"]
                for c in loaded.cases
            )
            stored = await store.suite_document(session, suite_id=existing.id, version=number)
            role = await self._role(session, actor, existing, existing.scrub_status)
            if (
                allows(role, "edit")
                and same_source
                and not clash
                and stored in (None, loaded.document)
            ):
                if stored is None:
                    await self._store_cases(session, actor, existing, loaded, hashes)
                    await self._store_version(
                        session,
                        existing,
                        loaded.document,
                        actor,
                        signature=signature,
                        bundle_scrub=manifest.scrub.status,
                    )
                return ImportedSuite(bundle_id=key, id=key, version=number, created=False)
        target, n, suite = key, 1, None
        while suite is None:
            suite = await store.create_suite(
                session, tenant_id=actor.tenant_id, key=target, name=str(loaded.document["name"]),
                owner_key=actor.key, source_instance=instance,
            )  # fmt: skip
            n += 1
            target = suite.key if suite else f"{key[:120]}-{n}"
        await self._store_cases(session, actor, suite, loaded, {})
        document = {**loaded.document, "id": target}
        await self._store_version(
            session,
            suite,
            document,
            actor,
            signature=signature,
            bundle_scrub=manifest.scrub.status,
        )
        return ImportedSuite(bundle_id=key, id=target, version=number, created=True)

    async def _store_cases(
        self,
        session: AsyncSession,
        actor: Subject,
        suite: store.SuiteRow,
        loaded: LoadedSuite,
        stored: dict[tuple[str, int], str],
    ) -> None:
        for case in loaded.cases:
            model = _case_model(case)
            if (model.id, model.revision) in stored:
                continue
            shas = blob_refs(case)
            await store.add_case_revision(
                session, suite=suite, key=model.id, revision=model.revision, document=case,
                sha256=str(case["hash"]), fidelity=model.fidelity.level,
                flags=[f.reason for f in model.review_flags or []], blob_shas=shas,
                scrub_status=await _with_blobs(session, actor.tenant_id, model, shas),
                created_by=actor.key,
            )  # fmt: skip

    async def _import_run(
        self,
        session: AsyncSession,
        actor: Subject,
        manifest: Manifest,
        loaded: LoadedRun,
        renamed: dict[str, str],
        signature: str,
    ) -> None:
        """Store a run as a record of work done elsewhere: never as one to carry on here."""
        run = Run.model_validate(loaded.document)
        key = renamed.get(run.suite.id, run.suite.id)
        if key in renamed.values():  # `_import_suite` just stored it for this actor
            suite = await store.suite_by_key(session, tenant_id=actor.tenant_id, key=key)
            assert suite is not None
        else:
            suite, _ = await self._suite(session, actor, key, "edit")
        version = await store.suite_document(session, suite_id=suite.id, version=run.suite.version)
        if version is None:
            raise EvalRefused(f"Run {run.id} needs suite {key} version {run.suite.version}.")
        pins = {(str(c["id"]), c["revision"], c["hash"]) for c in _pins_of(version)}
        items = [RunItem.model_validate(i) for i in loaded.items]
        if stray := sorted(
            {i.case.id for i in items if (i.case.id, i.case.revision, i.case.hash) not in pins}
        ):
            raise EvalRefused(f"Run {run.id} has cases suite {key} does not pin.", stray)
        document = {k: v for k, v in loaded.document.items() if k != "approval"}
        document["suite"] = {"id": key, "version": run.suite.version}
        if run.status in _UNFINISHED_RUN:
            document["status"] = "cancelled"
        if run.origin.kind == "local":
            origin = RunOrigin(
                kind="imported", deployment=manifest.source.label,
                daimon_version=manifest.source.daimon_version,
            )  # fmt: skip
            document["origin"] = to_document(origin)
        documents = [
            {**raw, "status": "cancelled"} if item.status in ("pending", "running") else raw
            for raw, item in zip(loaded.items, items, strict=True)
        ]
        scrub = await store.version_scrub(session, suite_id=suite.id, version=run.suite.version)
        await store.add_run(
            session, tenant_id=actor.tenant_id, suite_id=suite.id, document=document,
            items=[(i, blob_refs(i)) for i in documents], scores=loaded.scores,
            reviews=loaded.reviews, scrub_status=_worst([manifest.scrub.status, scrub]),
            created_by=actor.key, signature=signature,
        )  # fmt: skip


def eval_service(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    anthropic: AsyncAnthropic | None,
) -> EvalService:
    """The service as every surface builds it: blob bytes in the deployment's artifact store
    when it has one, live arms checked against its agents."""
    return EvalService(
        sessions,
        settings,
        objects=build_artifact_store(settings.artifacts) if settings.artifacts else None,
        agents=agent_lookup(sessions, anthropic) if anthropic else None,
    )


# Default channel names are everyday words: faking them would rewrite ordinary text.
_COMMON_PLACES: Final = frozenset(
    {"general", "random", "announcements", "help", "support", "dev", "test", "chat", "team",
     "off-topic", "introductions", "welcome", "rules", "news", "questions", "feedback"}
)  # fmt: skip


def _deployment_entities(names: eval_scrub.DeploymentNames) -> KnownEntities:
    return KnownEntities(
        people=[
            PersonEntry(
                name=name, handles=[handle] if handle else [], ids=[uid] if uid else [],
                emails=[email] if email else [],
            )
            for name, handle, uid, email in names.people
        ],
        channels=[p for p in names.places if p.casefold().lstrip("#") not in _COMMON_PLACES],
        repos=names.repos,
    )  # fmt: skip


def _matches(entry: BlobEntry, data: bytes) -> bool:
    return sha256_hex(data) == entry.sha256 and len(data) == entry.size


def _measure(spool: IO[bytes]) -> tuple[int, str]:
    spool.seek(0)
    digest, size = hashlib.sha256(), 0
    while chunk := spool.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    spool.seek(0)
    return size, digest.hexdigest()


def _size(spool: IO[bytes]) -> int:
    return spool.seek(0, 2)


def _pins_of(suite: JsonObject) -> list[JsonObject]:
    cases = suite.get("cases")
    return [dict(c) for c in cases if isinstance(c, dict)] if isinstance(cases, list) else []


def _case_model(document: JsonObject) -> Case:
    return Case.model_validate(document)


def _case_scrub(case: Case) -> str | None:
    return case.provenance.scrub.status if case.provenance.scrub else None


async def _with_blobs(
    session: AsyncSession, tenant_id: uuid.UUID, case: Case, shas: Iterable[str]
) -> str | None:
    """How scrubbed a case is, files included: one that holds a raw file is at best `partial`,
    so it can be neither shared nor exported as scrubbed."""
    status = _case_scrub(case)
    if status != "scrubbed":
        return status
    rows = await store.blob_rows(session, tenant_id=tenant_id, shas=shas)
    return "partial" if any(r.scrub == "unscrubbed" for r in rows.values()) else status


def _labelled(
    suites: Sequence[LoadedSuite],
    runs: Sequence[LoadedRun],
    pinned: Sequence[JsonObject],
    secret: str,
) -> tuple[list[LoadedSuite], list[LoadedRun]]:
    """Suites and runs with people as labels. A case changed by that gets a new hash, which
    the suites pinning it and the run items running it follow."""
    rehashed: dict[str, str] = {}

    def case(document: JsonObject) -> JsonObject:
        labelled = _label_people(document, secret)
        labelled["hash"] = case_hash(labelled)
        rehashed[str(document["hash"])] = str(labelled["hash"])
        return labelled

    def pin(ref: JsonValue) -> JsonValue:
        if isinstance(ref, dict) and isinstance(ref.get("hash"), str):
            return {**ref, "hash": rehashed.get(str(ref["hash"]), ref["hash"])}
        return ref

    new_suites: list[LoadedSuite] = []
    for suite in suites:
        cases = [case(c) for c in suite.cases]
        document = _label_people(suite.document, secret)
        document["cases"] = [pin(r) for r in _pins_of(document)]
        new_suites.append(LoadedSuite(document, cases))
    for document in pinned:
        case(document)
    new_runs = [
        LoadedRun(
            _label_people(r.document, secret),
            [{**i, "case": pin(i.get("case"))} for i in r.items],
            [_label_people(s, secret) for s in r.scores],
            [_label_people(v, secret) for v in r.reviews],
        )
        for r in runs
    ]
    return new_suites, new_runs

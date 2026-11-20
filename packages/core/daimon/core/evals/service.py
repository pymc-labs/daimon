"""`EvalService`: what every surface (web, MCP, CLI) calls to work with evals.

It checks the `evals` feature and who may do what (`access`), and keeps every document as
the JSON the bundle format defines.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterable, Iterable, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Final, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from daimon.core.config import Settings
from daimon.core.evals import bundle as bundles
from daimon.core.evals.access import (
    Action,
    Share,
    Subject,
    SuiteRole,
    allows,
    held_groups,
    suite_role,
)
from daimon.core.evals.blobs import blob_path, blob_refs
from daimon.core.evals.format import (
    Arm,
    BlobEntry,
    BlobRef,
    Case,
    ItemCaseRef,
    JsonObject,
    Manifest,
    Run,
    RunItem,
    ScrubStatus,
    SourceInfo,
    Suite,
    SuiteDefaults,
    ToolAlias,
    case_hash,
    sha256_hex,
    to_document,
)
from daimon.core.evals.validate import (
    InvalidDocument,
    LoadedRun,
    LoadedSuite,
    case_problems,
    case_warnings,
    schema_problems,
    validate_bundle,
)
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.stores import evals as store
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CaseRef = ItemCaseRef
_SCRUB_ORDER: Final[tuple[ScrubStatus, ...]] = ("scrubbed", "partial", "unscrubbed")
OFF: Final = "Evals are off here. Ask an admin or an owner to turn them on."


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
    passphrase: str | None = None
    """Set: the bundle is encrypted with it (`*.daimon-eval.enc`)."""
    include_identities: bool = False
    """Owners and admins only: keep who reviewed and owns instead of neutral labels."""


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


def _worst(statuses: Iterable[str | None]) -> ScrubStatus:
    found = [s for s in statuses if s in _SCRUB_ORDER]
    return max(found, key=_SCRUB_ORDER.index, default="scrubbed")  # pyright: ignore[reportReturnType]


def _shares(rows: Iterable[store.ShareRow]) -> list[Share]:
    return [Share(kind=r.who_kind, id=r.who, role=r.role) for r in rows]  # pyright: ignore[reportArgumentType]


def _daimon_version() -> str | None:
    try:
        return version("daimon-core")
    except PackageNotFoundError:
        return None


def _label_people(documents: Sequence[JsonObject]) -> list[JsonObject]:
    """Copies with every person, group and agent reference replaced by a stable label."""
    labels: dict[tuple[str, str], str] = {}

    def walk(node: JsonValue) -> JsonValue:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        kind, ref = node.get("kind"), node.get("id")
        is_ref = node.keys() <= {"kind", "id", "display_name"} and isinstance(ref, str)
        if is_ref and kind in ("person", "group", "agent"):
            key = (str(kind), str(ref))
            labels.setdefault(key, f"{kind}-{sum(k[0] == kind for k in labels) + 1}")
            return {"kind": "label", "id": labels[key]}
        return {k: walk(v) for k, v in node.items()}

    out: list[JsonObject] = []
    for document in documents:
        walked = walk(document)
        assert isinstance(walked, dict)
        out.append(walked)
    return out


def blind_item(document: JsonObject, labels: dict[str, str]) -> JsonObject:
    """An item as a blind reviewer sees it: the arm as its neutral label, no model names."""
    item = RunItem.model_validate(document)
    events = item.transcript.events if item.transcript else None
    update: dict[str, object] = {"arm_id": labels.get(item.arm_id, "arm")}
    if item.transcript and events is not None:
        quiet = [e.model_copy(update={"model": None}) for e in events]
        update["transcript"] = item.transcript.model_copy(update={"events": quiet})
    if item.enclosure:
        update["enclosure"] = item.enclosure.model_copy(update={"backend": None})
    return to_document(item.model_copy(update=update))


class EvalService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessions = sessions
        self._settings = settings

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
        self, session: AsyncSession, actor: Subject, suite: store.SuiteRow
    ) -> SuiteRole | None:
        shares = _shares((await store.shares_of(session, [suite.id])).get(suite.id, []))
        groups = await held_groups(actor, shares)
        return suite_role(actor, owner_key=suite.owner_key, shares=shares, groups=groups)

    async def _suite(
        self, session: AsyncSession, actor: Subject, key: str, action: Action
    ) -> tuple[store.SuiteRow, SuiteRole]:
        suite = await store.suite_by_key(session, tenant_id=actor.tenant_id, key=key)
        role = suite and suite.archived_at is None and await self._role(session, actor, suite)
        if suite is None or not role:
            raise EvalNotFound(f"No suite {key} here.")
        if not allows(role, action):
            raise EvalForbidden(f"You can't {action} this suite. Ask its owner.")
        return suite, role

    async def _run(
        self, session: AsyncSession, actor: Subject, run: store.RunRow | None, action: Action
    ) -> tuple[store.RunRow, SuiteRole]:
        suite = run and (await store.suites_by_id(session, [run.suite_id])).get(run.suite_id)
        role = suite and await self._role(session, actor, suite)
        if run is None or not role:
            raise EvalNotFound("No such run here.")
        if not allows(role, action):
            raise EvalForbidden(f"You can't {action} this run. Ask its suite's owner.")
        return run, role

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
            if role is not None:
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
            suite, _ = await self._suite(session, actor, suite_id, "view")
            document = await store.suite_document(
                session, suite_id=suite.id, version=version or suite.current_version
            )
        if document is None:
            raise EvalNotFound(f"Suite {suite_id} has no version {version}.")
        return Suite.model_validate(document)

    async def cases(
        self, actor: Subject, suite_id: str, version: int | None = None
    ) -> list[CaseSummary]:
        await self._check_on(actor)
        async with self._sessions() as session:
            suite, _ = await self._suite(session, actor, suite_id, "view")
            rows = await store.version_cases(
                session, suite_id=suite.id, version=version or suite.current_version
            )
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
            suite, _ = await self._suite(session, actor, suite_id, "view")
            row = await store.case_revision(
                session, suite_id=suite.id, key=case_id, revision=revision
            )
        if row is None:
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
            existing = await store.suite_by_key(session, tenant_id=actor.tenant_id, key=draft.id)
            if new and existing is not None:
                raise EvalRefused(f"A suite {draft.id} already exists. Pick another id.")
            if new is False and existing is None:
                raise EvalNotFound(f"No suite {draft.id} here.")
            if existing is None:
                if draft.cases:
                    raise EvalRefused("A new suite starts with no cases. Add them one by one.")
                suite = await store.create_suite(
                    session,
                    tenant_id=actor.tenant_id,
                    key=draft.id,
                    name=draft.name,
                    owner_key=actor.key,
                )
                pinned: list[store.CaseRevisionRow] = []
                previous: JsonObject = {}
            else:
                suite, _ = await self._suite(session, actor, draft.id, "edit")
                pinned = await store.version_cases(
                    session, suite_id=suite.id, version=suite.current_version
                )
                previous = (
                    await store.suite_document(
                        session, suite_id=suite.id, version=suite.current_version
                    )
                    or {}
                )
            refs = _pins_of(previous)
            if draft.cases is not None:
                refs = await self._picked(session, suite, draft.cases, refs)
            document = to_document(
                Suite(
                    id=draft.id, version=suite.current_version + 1, name=draft.name,
                    description=draft.description, tags=draft.tags, created_at=datetime.now(UTC),
                    cases=refs, tool_aliases=draft.tool_aliases, defaults=draft.defaults,  # pyright: ignore[reportArgumentType]
                )
            )  # fmt: skip
            statuses = {(r.key, r.revision): r.scrub_status for r in pinned}
            await self._store_version(session, suite, document, actor, statuses)
        return Suite.model_validate(document)

    async def _picked(
        self,
        session: AsyncSession,
        suite: store.SuiteRow,
        picks: Sequence[SuiteCasePick],
        refs: list[JsonObject],
    ) -> list[JsonObject]:
        current = {str(r["id"]): r for r in refs}
        picked: list[JsonObject] = []
        for pick in picks:
            row = current.get(pick.id)
            if row is None:
                found = await store.case_revision(session, suite_id=suite.id, key=pick.id)
                if found is None:
                    raise EvalNotFound(f"No case {pick.id} in suite {suite.key}.")
                row = {"id": pick.id, "revision": found.revision, "hash": found.sha256}
            picked.append({**row, "weight": pick.weight, "split": pick.split})
        return picked

    async def _store_version(
        self,
        session: AsyncSession,
        suite: store.SuiteRow,
        document: JsonObject,
        actor: Subject,
        statuses: dict[tuple[str, int], str | None],
    ) -> None:
        if problems := schema_problems("Suite", document, "suite"):
            raise EvalRefused("This suite is not valid.", problems)
        ids = await store.case_ids(session, suite_id=suite.id)
        refs = _pins_of(document)
        pins = [
            store.PinRow(
                case_id=ids[str(r["id"])], revision=int(str(r["revision"])),
                weight=float(str(r.get("weight", 1))), split=str(r.get("split", "test")),
            )
            for r in refs
        ]  # fmt: skip
        scrub = _worst(statuses.get((str(r["id"]), int(str(r["revision"])))) for r in refs)
        version = int(str(document["version"]))
        await store.add_suite_version(
            session, suite, version=version, document=document, pins=pins, scrub_status=scrub,
            created_by=actor.key,
        )  # fmt: skip

    async def put_case(self, actor: Subject, suite_id: str, case: JsonObject) -> CaseRef:
        """Store `case` as the next revision of its id and pin it in a new suite version."""
        await self._check_on(actor)
        key = str(case.get("id"))
        async with self._sessions() as session, session.begin():
            suite, _ = await self._suite(session, actor, suite_id, "edit")
            latest = await store.case_revision(session, suite_id=suite.id, key=key)
            document = {k: v for k, v in case.items() if k != "hash"}
            document["revision"] = latest.revision + 1 if latest else 1
            document["hash"] = case_hash(document)
            if problems := case_problems(document):
                raise EvalRefused("This case is not valid.", problems)
            shas = blob_refs(document)
            missing = (
                shas - (await store.blob_rows(session, tenant_id=actor.tenant_id, shas=shas)).keys()
            )
            if missing:
                raise EvalRefused(
                    "This case uses files this deployment does not have.", sorted(missing)
                )
            model = _case_model(document)
            scrub = model.provenance.scrub.status if model.provenance.scrub else None
            await store.add_case_revision(
                session, suite=suite, key=key, revision=model.revision, document=document,
                sha256=str(document["hash"]), fidelity=model.fidelity.level,
                flags=[f.reason for f in model.review_flags or []], blob_shas=shas,
                scrub_status=scrub, created_by=actor.key,
            )  # fmt: skip
            pinned = await store.version_cases(
                session, suite_id=suite.id, version=suite.current_version
            )
            previous = (
                await store.suite_document(
                    session, suite_id=suite.id, version=suite.current_version
                )
                or {}
            )
            pin: JsonObject = {"id": key, "revision": model.revision, "hash": str(document["hash"])}
            refs = _pins_of(previous)
            at = next((i for i, r in enumerate(refs) if r["id"] == key), None)
            if at is None:
                refs.append(pin)
            else:
                refs[at] = {**refs[at], **pin}
            suite_doc: JsonObject = {**previous, "version": suite.current_version + 1}
            suite_doc["cases"] = list[JsonValue](refs)
            if not previous:
                suite_doc |= {"schema": "daimon.eval.suite/1", "id": suite.key, "name": suite.name}
            statuses: dict[tuple[str, int], str | None] = {
                (r.key, r.revision): r.scrub_status for r in pinned
            }
            statuses[(key, model.revision)] = scrub
            await self._store_version(session, suite, suite_doc, actor, statuses)
        return CaseRef(id=key, revision=model.revision, hash=str(document["hash"]))

    async def share(self, actor: Subject, suite_id: str, shares: Sequence[Share]) -> None:
        """Replace who the suite is shared with."""
        await self._check_on(actor)
        async with self._sessions() as session, session.begin():
            suite, _ = await self._suite(session, actor, suite_id, "share")
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
        """Create an arm, or replace one its creator or an admin owns."""
        await self._check_on(actor)
        document = to_document(arm)
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

    async def run(self, actor: Subject, run_id: str) -> Run:
        await self._check_on(actor)
        async with self._sessions() as session:
            found = await store.run_by_key(session, tenant_id=actor.tenant_id, key=run_id)
            run, _ = await self._run(session, actor, found, "view")
        return Run.model_validate(run.document)

    async def items(self, actor: Subject, run_id: str, page: Page) -> list[RunItemSummary]:
        await self._check_on(actor)
        async with self._sessions() as session:
            found = await store.run_by_key(session, tenant_id=actor.tenant_id, key=run_id)
            run, _ = await self._run(session, actor, found, "view")
            rows = await store.items_of_run(
                session, run_id=run.id, offset=page.offset, limit=page.limit
            )
        return [
            RunItemSummary(
                id=str(r.id), key=r.key, case_id=r.case_key, case_revision=r.case_revision,
                arm_id=r.arm_key, repeat=r.repeat, status=r.status,
            )
            for r in rows
        ]  # fmt: skip

    async def item(self, actor: Subject, item_id: str, *, blind_for: str | None = None) -> RunItem:
        """One run item; `blind_for` (a reviewer) hides which arm and model produced it."""
        await self._check_on(actor)
        try:
            item_uuid = uuid.UUID(item_id)
        except ValueError:
            raise EvalNotFound("No such item here.") from None
        async with self._sessions() as session:
            row = await store.item_by_id(session, tenant_id=actor.tenant_id, item_id=item_uuid)
            if row is None:
                raise EvalNotFound("No such item here.")
            run, _ = await self._run(
                session, actor, await store.run_by_id(session, row.run_id), "view"
            )
        document = row.document
        if blind_for is not None:
            labels = Run.model_validate(run.document).blind_labels or {}
            document = blind_item(document, labels)
        return RunItem.model_validate(document)

    async def blob(self, actor: Subject, sha256: str) -> tuple[store.BlobRow, bytes]:
        """A blob the actor may read: one their suites, runs or exports reference."""
        await self._check_on(actor)
        async with self._sessions() as session:
            rows = await store.blob_rows(session, tenant_id=actor.tenant_id, shas=[sha256])
            readable = actor.is_admin or bool(
                await store.exported_by(session, tenant_id=actor.tenant_id, sha256=sha256)
                & actor.ids
            )
            if sha256 in rows and not readable:
                ids = await store.suites_using_blob(
                    session, tenant_id=actor.tenant_id, sha256=sha256
                )
                for suite in (await store.suites_by_id(session, ids)).values():
                    if allows(await self._role(session, actor, suite), "view"):
                        readable = True
                        break
            content = await store.blob_content(session, tenant_id=actor.tenant_id, sha256=sha256)
        if sha256 not in rows or content is None or not readable:
            raise EvalNotFound("No such file here.")
        return rows[sha256], content

    # ---------- bundles ----------

    def _instance(self, tenant_id: uuid.UUID) -> str:
        """An opaque, stable name for this workspace as a bundle source."""
        return sha256_hex(f"daimon-evals:{tenant_id}".encode())[:16]

    async def export_bundle(self, actor: Subject, request: ExportRequest) -> BlobRef:
        """Write the chosen suites and runs as a bundle, keep it as a blob, return its ref."""
        await self._check_on(actor)
        suites: list[LoadedSuite] = []
        runs: list[LoadedRun] = []
        statuses: list[str | None] = []
        roles: list[SuiteRole] = []
        async with self._sessions() as session:
            for pick in request.suites:
                suite, role = await self._suite(session, actor, pick.id, "export")
                number = pick.version or suite.current_version
                document = await store.suite_document(session, suite_id=suite.id, version=number)
                if document is None:
                    raise EvalNotFound(f"Suite {pick.id} has no version {number}.")
                cases = await store.version_cases(session, suite_id=suite.id, version=number)
                statuses += [c.scrub_status for c in cases]
                roles.append(role)
                suites.append(LoadedSuite(document, [c.document for c in cases]))
            for key in request.runs:
                found = await store.run_by_key(session, tenant_id=actor.tenant_id, key=key)
                run, role = await self._run(session, actor, found, "export")
                run_suite = (await store.suites_by_id(session, [run.suite_id]))[run.suite_id]
                statuses.append(run_suite.scrub_status)
                roles.append(role)
                items, scores, reviews = await store.run_parts(session, run_id=run.id)
                runs.append(LoadedRun(run.document, items, scores, reviews))
            if not suites and not runs:
                raise EvalRefused("Pick at least one suite or run to export.")
            documents: list[JsonValue] = [d for s in suites for d in (s.document, *s.cases)]
            documents += [d for r in runs for d in (r.document, *r.items, *r.scores, *r.reviews)]
            shas = blob_refs(documents)
            blob_rows = await store.blob_rows(session, tenant_id=actor.tenant_id, shas=shas)
            contents = {
                sha: await store.blob_content(session, tenant_id=actor.tenant_id, sha256=sha)
                for sha in shas
            }
        status = _worst(
            [*statuses, *("unscrubbed" for b in blob_rows.values() if b.scrub == "unscrubbed")]
        )
        owner = actor.is_admin or all(r == "owner" for r in roles)
        if status != "scrubbed" and not owner:
            raise EvalForbidden("Only the owner or an admin can export what is not scrubbed.")
        if request.include_identities and not owner:
            raise EvalForbidden("Only the owner or an admin can export who did what.")
        if not request.include_identities:
            suites, runs = _labelled(suites, runs)
        missing = [sha for sha, data in contents.items() if data is None or sha not in blob_rows]
        if missing:
            raise EvalRefused("Some files these documents use are missing.", missing)
        blobs = {
            BlobEntry(
                sha256=sha, size=row.size, media_type=row.media_type, scrub=row.scrub,  # pyright: ignore[reportArgumentType]
                needs_human_check=row.needs_human_check,
            ): contents[sha] or b""
            for sha, row in blob_rows.items()
        }  # fmt: skip
        now = datetime.now(UTC)
        header = bundles.BundleHeader(
            id=f"bundle-{uuid.uuid4().hex[:16]}",
            created_at=now.replace(microsecond=0),
            title=request.title,
            source=SourceInfo(
                kind="deployment",
                label=self._settings.evals.source_label,
                instance=self._instance(actor.tenant_id),
                daimon_version=_daimon_version(),
            ),  # fmt: skip
            scrub_status=status,
            includes_identities=request.include_identities,
        )
        key = self._settings.evals.signing_key
        files = bundles.build_files(
            header, suites, runs, blobs,
            signing_key=bundles.private_key(key.get_secret_value()) if key else None,
        )  # fmt: skip
        data = bundles.to_zip(files)
        name, media_type = f"{header.id}.daimon-eval.zip", "application/zip"
        if request.passphrase:
            data = bundles.encrypt(data, request.passphrase)
            name, media_type = f"{header.id}.daimon-eval.enc", "application/octet-stream"
        sha = sha256_hex(data)
        manifest = json.loads(files[bundles.MANIFEST_PATH])
        async with self._sessions() as session, session.begin():
            blob = store.BlobRow(
                sha256=sha, size=len(data), media_type=media_type, needs_human_check=False,
                scrub="unscrubbed" if status != "scrubbed" else "clean",
            )  # fmt: skip
            await store.put_blob(session, tenant_id=actor.tenant_id, blob=blob, content=data)
            await store.record_bundle(
                session, tenant_id=actor.tenant_id, direction="export", manifest=manifest,
                actor_key=actor.key, blob_sha256=sha,
            )  # fmt: skip
        return BlobRef(sha256=sha, size=len(data), media_type=media_type, name=name)

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
        chunks: list[bytes] = []
        size = 0
        async for chunk in data:
            size += len(chunk)
            if size > limit:
                raise EvalRefused(f"The bundle is over {self._settings.evals.max_bundle_mb} MiB.")
            chunks.append(chunk)
        try:
            source = bundles.open_bundle(b"".join(chunks), passphrase=passphrase, max_bytes=limit)
            report = validate_bundle(source)
            report.raise_for_problems()
            raw_manifest = json.loads(source.read(bundles.MANIFEST_PATH))
            signature = bundles.verify(raw_manifest, self._trusted_keys())
        except InvalidDocument as invalid:
            raise EvalRefused("This bundle is not valid.", invalid.problems) from None
        manifest = report.manifest
        assert manifest is not None
        if refusal := bundles.scrub_refusal(manifest, accept_unscrubbed=accept_unscrubbed):
            raise EvalForbidden(refusal)
        async with self._sessions() as session, session.begin():
            for entry in manifest.blobs:
                row = store.BlobRow.model_validate(entry.model_dump())
                content = source.read(blob_path(entry.sha256))
                await store.put_blob(session, tenant_id=actor.tenant_id, blob=row, content=content)
            imported = [
                await self._import_suite(session, actor, manifest, s) for s in report.suites
            ]
            renamed = {s.bundle_id: s.id for s in imported}
            done: list[str] = []
            skipped: list[str] = []
            for run in report.runs:
                run_id = str(run.document["id"])
                if await store.run_by_key(session, tenant_id=actor.tenant_id, key=run_id):
                    skipped.append(run_id)
                    continue
                await self._import_run(session, actor, run, renamed)
                done.append(run_id)
            await store.record_bundle(
                session, tenant_id=actor.tenant_id, direction="import", manifest=raw_manifest,
                actor_key=actor.key,
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
        self, session: AsyncSession, actor: Subject, manifest: Manifest, loaded: LoadedSuite
    ) -> ImportedSuite:
        """A suite lands as a new version of the same suite from the same source when the actor
        may edit it and no case revision conflicts; otherwise as a new suite."""
        key, number = str(loaded.document["id"]), int(str(loaded.document["version"]))
        instance = manifest.source.instance
        existing = await store.suite_by_key(session, tenant_id=actor.tenant_id, key=key)
        if existing is not None:
            same_source = (existing.source_instance or self._instance(actor.tenant_id)) == instance
            hashes = await store.case_hashes(session, suite_id=existing.id)
            clash = any(
                hashes.get((str(c["id"]), int(str(c["revision"]))), c["hash"]) != c["hash"]
                for c in loaded.cases
            )
            stored = await store.suite_document(session, suite_id=existing.id, version=number)
            role = await self._role(session, actor, existing)
            if (
                allows(role, "edit")
                and same_source
                and not clash
                and stored in (None, loaded.document)
            ):
                if stored is None:
                    await self._store_cases(session, actor, existing, loaded, hashes)
                    await self._store_imported_version(
                        session, actor, existing, loaded.document, loaded
                    )
                return ImportedSuite(bundle_id=key, id=key, version=number, created=False)
        target = key
        n = 1
        while existing is not None:
            n += 1
            target = f"{key[:120]}-{n}"
            existing = await store.suite_by_key(session, tenant_id=actor.tenant_id, key=target)
        document = {**loaded.document, "id": target}
        suite = await store.create_suite(
            session, tenant_id=actor.tenant_id, key=target, name=str(document["name"]),
            owner_key=actor.key, source_instance=instance,
        )  # fmt: skip
        await self._store_cases(session, actor, suite, loaded, {})
        await self._store_imported_version(session, actor, suite, document, loaded)
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
            await store.add_case_revision(
                session, suite=suite, key=model.id, revision=model.revision, document=case,
                sha256=str(case["hash"]), fidelity=model.fidelity.level,
                flags=[f.reason for f in model.review_flags or []], blob_shas=blob_refs(case),
                scrub_status=model.provenance.scrub.status if model.provenance.scrub else None,
                created_by=actor.key,
            )  # fmt: skip

    async def _store_imported_version(
        self,
        session: AsyncSession,
        actor: Subject,
        suite: store.SuiteRow,
        document: JsonObject,
        loaded: LoadedSuite,
    ) -> None:
        statuses: dict[tuple[str, int], str | None] = {}
        for case in loaded.cases:
            model = _case_model(case)
            statuses[(model.id, model.revision)] = (
                model.provenance.scrub.status if model.provenance.scrub else None
            )
        refreshed = await store.suite_by_key(session, tenant_id=suite.tenant_id, key=suite.key)
        assert refreshed is not None
        await self._store_version(session, refreshed, document, actor, statuses)

    async def _import_run(
        self, session: AsyncSession, actor: Subject, loaded: LoadedRun, renamed: dict[str, str]
    ) -> None:
        pin = Run.model_validate(loaded.document).suite
        key = renamed.get(pin.id, pin.id)
        suite, _ = await self._suite(session, actor, key, "edit")
        if await store.suite_document(session, suite_id=suite.id, version=pin.version) is None:
            raise EvalRefused(
                f"Run {loaded.document['id']} needs suite {key} version {pin.version}."
            )
        document = dict(loaded.document)
        if key != pin.id:
            document["suite"] = {"id": key, "version": pin.version}
        await store.add_run(
            session, tenant_id=actor.tenant_id, suite_id=suite.id, document=document,
            items=[(i, blob_refs(i)) for i in loaded.items], scores=loaded.scores,
            reviews=loaded.reviews, created_by=actor.key,
        )  # fmt: skip


def _pins_of(suite: JsonObject) -> list[JsonObject]:
    cases = suite.get("cases")
    return [dict(c) for c in cases if isinstance(c, dict)] if isinstance(cases, list) else []


def _case_model(document: JsonObject) -> Case:
    return Case.model_validate(document)


def _labelled(
    suites: Sequence[LoadedSuite], runs: Sequence[LoadedRun]
) -> tuple[list[LoadedSuite], list[LoadedRun]]:
    """Suites and runs with people as labels, numbered across the whole bundle. Cases are left
    as they are: their hash covers them, and scrubbing has already replaced people there."""
    flat = [s.document for s in suites]
    for run in runs:
        flat += [run.document, *run.scores, *run.reviews]
    labelled = iter(_label_people(flat))
    new_suites = [LoadedSuite(next(labelled), s.cases) for s in suites]
    new_runs = [
        LoadedRun(
            next(labelled), r.items, [next(labelled) for _ in r.scores],
            [next(labelled) for _ in r.reviews],
        )
        for r in runs
    ]  # fmt: skip
    return new_suites, new_runs

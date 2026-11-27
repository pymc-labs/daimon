"""Store for the eval tables: suites and their versions, case revisions, shares, blob
metadata, arms, runs with their items, scores and reviews, and the bundles moved in and out."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime

from daimon.core._models import (
    EvalArm,
    EvalBlob,
    EvalBundle,
    EvalCase,
    EvalCaseRevision,
    EvalComment,
    EvalInstance,
    EvalReviewQueue,
    EvalRun,
    EvalRunItem,
    EvalScore,
    EvalShare,
    EvalSuite,
    EvalSuiteVersion,
    EvalSuiteVersionCase,
)
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import case, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

Document = dict[str, JsonValue]


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class SuiteRow(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    name: str
    owner_key: str
    current_version: int
    scrub_status: str
    source_instance: str | None
    archived_at: datetime | None
    created_at: datetime


class ShareRow(_Row):
    who_kind: str
    who: str
    role: str


class CaseRevisionRow(_Row):
    case_id: uuid.UUID
    key: str
    revision: int
    document: Document
    sha256: str
    scrub_status: str | None


class PinRow(_Row):
    case_id: uuid.UUID
    revision: int
    weight: float
    split: str


class BlobRow(_Row):
    sha256: str
    size: int
    media_type: str
    scrub: str
    needs_human_check: bool


class ArmRow(_Row):
    id: uuid.UUID
    key: str
    label: str
    kind: str
    document: Document
    created_by: str | None


class RunRow(_Row):
    id: uuid.UUID
    key: str
    suite_id: uuid.UUID
    suite_version: int
    status: str
    origin: str
    scrub_status: str
    signature: str | None
    document: Document


class ItemRow(_Row):
    id: uuid.UUID
    run_id: uuid.UUID
    key: str
    case_key: str
    case_revision: int
    arm_key: str
    repeat: int
    status: str
    document: Document


# ---------- suites ----------


async def list_suites(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[SuiteRow]:
    rows = await session.scalars(
        select(EvalSuite)
        .where(EvalSuite.tenant_id == tenant_id, EvalSuite.archived_at.is_(None))
        .order_by(EvalSuite.name, EvalSuite.key)
    )
    return [SuiteRow.model_validate(r) for r in rows]


async def suite_by_key(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str, lock: bool = False
) -> SuiteRow | None:
    """`lock` holds the row until the transaction ends, so writes to one suite queue up."""
    query = select(EvalSuite).where(EvalSuite.tenant_id == tenant_id, EvalSuite.key == key)
    row = await session.scalar(query.with_for_update() if lock else query)
    return row and SuiteRow.model_validate(row)


async def suites_by_id(
    session: AsyncSession, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, SuiteRow]:
    rows = await session.scalars(select(EvalSuite).where(EvalSuite.id.in_(list(ids))))
    return {r.id: SuiteRow.model_validate(r) for r in rows}


async def create_suite(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    name: str,
    owner_key: str,
    source_instance: str | None = None,
) -> SuiteRow | None:
    """A new suite with no versions, or None when the key is taken."""
    row = await session.scalar(
        insert(EvalSuite)
        .values(
            tenant_id=tenant_id, key=key, name=name, owner_key=owner_key, current_version=0,
            scrub_status="scrubbed", source_instance=source_instance,
        )
        .on_conflict_do_nothing(constraint="uq_eval_suites_tenant_key")
        .returning(EvalSuite)
    )  # fmt: skip
    return row and SuiteRow.model_validate(row)


async def add_suite_version(
    session: AsyncSession,
    suite: SuiteRow,
    *,
    version: int,
    document: Document,
    pins: Sequence[PinRow],
    scrub_status: str,
    created_by: str | None,
    signature: str | None = None,
) -> None:
    """Store one suite version with its pins; the suite's latest moves to it if newer."""
    session.add(
        EvalSuiteVersion(
            suite_id=suite.id, version=version, tenant_id=suite.tenant_id, document=document,
            scrub_status=scrub_status, signature=signature, created_by=created_by,
        )
    )  # fmt: skip
    await session.flush()
    session.add_all(
        EvalSuiteVersionCase(
            suite_id=suite.id, version=version, position=position, tenant_id=suite.tenant_id,
            case_id=pin.case_id, revision=pin.revision, weight=pin.weight, split=pin.split,
        )
        for position, pin in enumerate(pins)
    )  # fmt: skip
    name = document.get("name")
    if version > suite.current_version:
        await session.execute(
            update(EvalSuite)
            .where(EvalSuite.id == suite.id)
            .values(
                current_version=version,
                name=name if isinstance(name, str) else suite.name,
                scrub_status=scrub_status,
            )
        )
    await session.flush()


async def suite_document(
    session: AsyncSession, *, suite_id: uuid.UUID, version: int
) -> Document | None:
    row = await session.scalar(
        select(EvalSuiteVersion.document).where(
            EvalSuiteVersion.suite_id == suite_id, EvalSuiteVersion.version == version
        )
    )
    return row


async def version_scrub(session: AsyncSession, *, suite_id: uuid.UUID, version: int) -> str | None:
    """How scrubbed a suite version's cases are; None when there is no such version."""
    return await session.scalar(
        select(EvalSuiteVersion.scrub_status).where(
            EvalSuiteVersion.suite_id == suite_id, EvalSuiteVersion.version == version
        )
    )


async def revision_scrubs(
    session: AsyncSession, keys: Sequence[tuple[uuid.UUID, int]]
) -> list[str | None]:
    """The stored scrub status of each (case id, revision); a missing one reads as None."""
    if not keys:
        return []
    rows = await session.execute(
        select(EvalCaseRevision.case_id, EvalCaseRevision.revision, EvalCaseRevision.scrub_status)
        .where(tuple_(EvalCaseRevision.case_id, EvalCaseRevision.revision).in_(list(keys)))
    )  # fmt: skip
    found = {(c, r): s for c, r, s in rows.tuples()}
    return [found.get(k) for k in keys]


async def version_cases(
    session: AsyncSession, *, suite_id: uuid.UUID, version: int
) -> list[CaseRevisionRow]:
    """The case revisions a suite version pins, in suite order."""
    rows = await session.execute(
        select(EvalCaseRevision, EvalCase.key)
        .join(EvalCase, EvalCase.id == EvalCaseRevision.case_id)
        .join(
            EvalSuiteVersionCase,
            (EvalSuiteVersionCase.case_id == EvalCaseRevision.case_id)
            & (EvalSuiteVersionCase.revision == EvalCaseRevision.revision),
        )
        .where(EvalSuiteVersionCase.suite_id == suite_id, EvalSuiteVersionCase.version == version)
        .order_by(EvalSuiteVersionCase.position)
    )
    return [_revision(r, key) for r, key in rows.tuples()]


def _revision(row: EvalCaseRevision, key: str) -> CaseRevisionRow:
    return CaseRevisionRow(
        case_id=row.case_id, key=key, revision=row.revision, document=row.document,
        sha256=row.sha256, scrub_status=row.scrub_status,
    )  # fmt: skip


async def case_revision(
    session: AsyncSession, *, suite_id: uuid.UUID, key: str, revision: int | None = None
) -> CaseRevisionRow | None:
    """One revision of a case; its latest when `revision` is None."""
    case = await session.scalar(
        select(EvalCase).where(EvalCase.suite_id == suite_id, EvalCase.key == key)
    )
    if case is None:
        return None
    row = await session.get(EvalCaseRevision, (case.id, revision or case.current_revision))
    return row and _revision(row, key)


async def add_case_revision(
    session: AsyncSession,
    *,
    suite: SuiteRow,
    key: str,
    revision: int,
    document: Document,
    sha256: str,
    fidelity: str,
    flags: Sequence[str],
    blob_shas: Iterable[str],
    scrub_status: str | None,
    created_by: str | None,
) -> uuid.UUID:
    """Store a case revision, making the case on first use; returns the case id."""
    case_id = await session.scalar(
        insert(EvalCase)
        .values(tenant_id=suite.tenant_id, suite_id=suite.id, key=key, current_revision=revision)
        .on_conflict_do_update(
            constraint="uq_eval_cases_suite_key",
            set_={"current_revision": func.greatest(EvalCase.current_revision, revision)},
        )
        .returning(EvalCase.id)
    )
    assert case_id is not None
    session.add(
        EvalCaseRevision(
            case_id=case_id, revision=revision, tenant_id=suite.tenant_id, document=document,
            sha256=sha256, fidelity=fidelity, flags=list(flags), blob_shas=sorted(blob_shas),
            scrub_status=scrub_status, created_by=created_by,
        )
    )  # fmt: skip
    await session.flush()
    return case_id


async def case_hashes(session: AsyncSession, *, suite_id: uuid.UUID) -> dict[tuple[str, int], str]:
    """Every stored case revision of a suite: (case id, revision) to hash."""
    rows = await session.execute(
        select(EvalCase.key, EvalCaseRevision.revision, EvalCaseRevision.sha256)
        .join(EvalCase, EvalCase.id == EvalCaseRevision.case_id)
        .where(EvalCase.suite_id == suite_id)
    )
    return {(key, revision): sha for key, revision, sha in rows.tuples()}


async def case_ids(session: AsyncSession, *, suite_id: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = await session.execute(
        select(EvalCase.key, EvalCase.id).where(EvalCase.suite_id == suite_id)
    )
    return dict(rows.tuples().all())


async def shares_of(
    session: AsyncSession, suite_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[ShareRow]]:
    found: dict[uuid.UUID, list[ShareRow]] = {}
    for row in await session.scalars(
        select(EvalShare).where(EvalShare.suite_id.in_(list(suite_ids)))
    ):
        found.setdefault(row.suite_id, []).append(ShareRow.model_validate(row))
    return found


async def set_shares(session: AsyncSession, suite: SuiteRow, shares: Iterable[ShareRow]) -> None:
    """Replace who a suite is shared with."""
    await session.execute(delete(EvalShare).where(EvalShare.suite_id == suite.id))
    session.add_all(
        EvalShare(
            suite_id=suite.id,
            tenant_id=suite.tenant_id,
            who_kind=s.who_kind,
            who=s.who,
            role=s.role,
        )
        for s in shares
    )
    await session.flush()


# ---------- blobs ----------


async def put_blob(
    session: AsyncSession, *, tenant_id: uuid.UUID, blob: BlobRow, content: bytes | None
) -> None:
    """Keep a blob's metadata, and its bytes when there is no artifact store (`content`).

    The same bytes kept again only get worse: unscrubbed and needing a check stick.
    """
    new = insert(EvalBlob).values(tenant_id=tenant_id, content=content, **blob.model_dump())
    await session.execute(
        new.on_conflict_do_update(
            index_elements=[EvalBlob.tenant_id, EvalBlob.sha256],
            set_={
                "scrub": case(
                    (new.excluded.scrub == "unscrubbed", "unscrubbed"), else_=EvalBlob.scrub
                ),
                "needs_human_check": EvalBlob.needs_human_check | new.excluded.needs_human_check,
                "content": func.coalesce(EvalBlob.content, new.excluded.content),
            },
        )
    )


async def blob_rows(
    session: AsyncSession, *, tenant_id: uuid.UUID, shas: Iterable[str]
) -> dict[str, BlobRow]:
    rows = await session.execute(
        select(
            EvalBlob.sha256,
            EvalBlob.size,
            EvalBlob.media_type,
            EvalBlob.scrub,
            EvalBlob.needs_human_check,
        ).where(EvalBlob.tenant_id == tenant_id, EvalBlob.sha256.in_(list(shas)))
    )
    return {r.sha256: BlobRow.model_validate(r) for r in rows}


async def blob_content(session: AsyncSession, *, tenant_id: uuid.UUID, sha256: str) -> bytes | None:
    return await session.scalar(
        select(EvalBlob.content).where(EvalBlob.tenant_id == tenant_id, EvalBlob.sha256 == sha256)
    )


async def blob_uses(
    session: AsyncSession, *, tenant_id: uuid.UUID, sha256: str
) -> list[tuple[uuid.UUID, str | None]]:
    """Each suite whose case revisions or run items reference a blob, with how scrubbed that
    revision or run is."""
    cases = (
        select(EvalCase.suite_id, EvalCaseRevision.scrub_status)
        .join(EvalCaseRevision, EvalCaseRevision.case_id == EvalCase.id)
        .where(
            EvalCaseRevision.tenant_id == tenant_id, EvalCaseRevision.blob_shas.contains([sha256])
        )
    )
    runs = (
        select(EvalRun.suite_id, EvalRun.scrub_status)
        .join(EvalRunItem, EvalRunItem.run_id == EvalRun.id)
        .where(EvalRunItem.tenant_id == tenant_id, EvalRunItem.blob_shas.contains([sha256]))
    )
    return list((await session.execute(cases.union(runs))).tuples())


async def suite_blobs(session: AsyncSession, *, suite_id: uuid.UUID) -> set[str]:
    """Every blob the suite's case revisions or run items reference."""
    cases = (
        select(func.unnest(EvalCaseRevision.blob_shas))
        .join(EvalCase, EvalCase.id == EvalCaseRevision.case_id)
        .where(EvalCase.suite_id == suite_id)
    )
    runs = (
        select(func.unnest(EvalRunItem.blob_shas))
        .join(EvalRun, EvalRun.id == EvalRunItem.run_id)
        .where(EvalRun.suite_id == suite_id)
    )
    return set(await session.scalars(cases.union(runs)))


async def record_bundle(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    direction: str,
    manifest: Document,
    actor_key: str,
    signature: str | None,
) -> None:
    session.add(
        EvalBundle(
            tenant_id=tenant_id, direction=direction, key=str(manifest["id"]), manifest=manifest,
            actor_key=actor_key, scrub_status=str(_get(manifest, "scrub", "status")),
            signature=signature,
        )
    )  # fmt: skip
    await session.flush()


async def instance_secret(session: AsyncSession, *, tenant_id: uuid.UUID) -> str:
    """The workspace's random secret behind its bundle source name and people labels."""
    await session.execute(
        insert(EvalInstance)
        .values(tenant_id=tenant_id, secret=secrets.token_hex(32))
        .on_conflict_do_nothing(index_elements=[EvalInstance.tenant_id])
    )
    found = await session.scalar(
        select(EvalInstance.secret).where(EvalInstance.tenant_id == tenant_id)
    )
    assert found is not None
    return found


def _get(document: Document, *path: str) -> JsonValue:
    node: JsonValue = document
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
    return node


# ---------- arms ----------


async def arm_by_key(session: AsyncSession, *, tenant_id: uuid.UUID, key: str) -> ArmRow | None:
    row = await session.scalar(
        select(EvalArm).where(EvalArm.tenant_id == tenant_id, EvalArm.key == key)
    )
    return row and ArmRow.model_validate(row)


async def list_arms(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[ArmRow]:
    rows = await session.scalars(
        select(EvalArm).where(EvalArm.tenant_id == tenant_id).order_by(EvalArm.key)
    )
    return [ArmRow.model_validate(r) for r in rows]


async def save_arm(
    session: AsyncSession, *, tenant_id: uuid.UUID, document: Document, created_by: str | None
) -> ArmRow:
    """Create an arm, or replace the one with the same id."""
    values = {
        "label": document["label"], "kind": document["kind"], "document": document,
        "config_hash": document.get("config_hash"),
    }  # fmt: skip
    row = await session.scalar(
        insert(EvalArm)
        .values(tenant_id=tenant_id, key=document["id"], created_by=created_by, **values)
        .on_conflict_do_update(constraint="uq_eval_arms_tenant_key", set_=values)
        .returning(EvalArm)
    )
    return ArmRow.model_validate(row)


# ---------- runs ----------


async def run_by_key(session: AsyncSession, *, tenant_id: uuid.UUID, key: str) -> RunRow | None:
    row = await session.scalar(
        select(EvalRun).where(EvalRun.tenant_id == tenant_id, EvalRun.key == key)
    )
    return row and RunRow.model_validate(row)


async def runs_of_suite(session: AsyncSession, *, suite_id: uuid.UUID) -> list[RunRow]:
    rows = await session.scalars(
        select(EvalRun).where(EvalRun.suite_id == suite_id).order_by(EvalRun.created_at)
    )
    return [RunRow.model_validate(r) for r in rows]


async def add_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    suite_id: uuid.UUID,
    document: Document,
    items: Sequence[tuple[Document, Iterable[str]]],
    scores: Sequence[Document],
    reviews: Sequence[Document],
    scrub_status: str,
    created_by: str | None,
    signature: str | None = None,
) -> uuid.UUID:
    """Store a whole run as a bundle carries it; items come with the blobs they reference.
    `scrub_status` is the worst of the run's content and the suite version it ran."""
    suite_pin = _get(document, "suite", "version")
    run = EvalRun(
        tenant_id=tenant_id, key=document["id"], suite_id=suite_id, suite_version=suite_pin,
        status=document["status"], origin=str(_get(document, "origin", "kind")), document=document,
        scrub_status=scrub_status, signature=signature, created_by=created_by,
    )  # fmt: skip
    session.add(run)
    await session.flush()
    session.add_all(
        EvalRunItem(
            tenant_id=tenant_id, run_id=run.id, key=item["id"], case_key=_get(item, "case", "id"),
            case_revision=_get(item, "case", "revision"), arm_key=item["arm_id"],
            repeat=item["repeat"], status=item["status"], document=item, blob_shas=sorted(shas),
        )
        for item, shas in items
    )  # fmt: skip
    session.add_all(
        EvalScore(
            tenant_id=tenant_id, run_id=run.id, key=score["id"], item_key=score.get("item_id"),
            criterion=score["criterion"], grader_kind=str(_get(score, "grader", "kind")),
            document=score,
        )
        for score in scores
    )  # fmt: skip
    for review in reviews:
        if review.get("kind") == "queue":
            session.add(
                EvalReviewQueue(
                    tenant_id=tenant_id, run_id=run.id, key=review["id"], document=review
                )
            )
        else:
            session.add(
                EvalComment(tenant_id=tenant_id, run_id=run.id, key=review["id"], document=review)
            )
    await session.flush()
    return run.id


async def run_parts(
    session: AsyncSession, *, run_id: uuid.UUID
) -> tuple[list[Document], list[Document], list[Document]]:
    """A run's items, scores and review records (queues, then comments) in stored order."""
    items = await session.scalars(
        select(EvalRunItem.document)
        .where(EvalRunItem.run_id == run_id)
        .order_by(EvalRunItem.created_at, EvalRunItem.key)
    )
    scores = await session.scalars(
        select(EvalScore.document)
        .where(EvalScore.run_id == run_id)
        .order_by(EvalScore.created_at, EvalScore.key)
    )
    queues = await session.scalars(
        select(EvalReviewQueue.document)
        .where(EvalReviewQueue.run_id == run_id)
        .order_by(EvalReviewQueue.created_at, EvalReviewQueue.key)
    )
    comments = await session.scalars(
        select(EvalComment.document)
        .where(EvalComment.run_id == run_id)
        .order_by(EvalComment.created_at, EvalComment.key)
    )
    return list(items), list(scores), [*queues, *comments]


async def items_of_run(
    session: AsyncSession, *, run_id: uuid.UUID, offset: int, limit: int
) -> list[ItemRow]:
    rows = await session.scalars(
        select(EvalRunItem).where(EvalRunItem.run_id == run_id)
        .order_by(EvalRunItem.created_at, EvalRunItem.key).offset(offset).limit(limit)
    )  # fmt: skip
    return [ItemRow.model_validate(r) for r in rows]


async def item_by_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, item_id: uuid.UUID
) -> ItemRow | None:
    row = await session.get(EvalRunItem, item_id)
    return ItemRow.model_validate(row) if row is not None and row.tenant_id == tenant_id else None


async def run_by_id(session: AsyncSession, run_id: uuid.UUID) -> RunRow | None:
    row = await session.get(EvalRun, run_id)
    return row and RunRow.model_validate(row)

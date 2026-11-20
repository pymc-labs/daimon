"""Store for the eval tables: suites and their versions, case revisions, shares, blobs, arms,
runs with their items, scores and reviews, and the bundles moved in and out."""

from __future__ import annotations

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
    EvalReviewAssignment,
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
from sqlalchemy import delete, func, select, update
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


class QueueRow(_Row):
    id: uuid.UUID
    run_id: uuid.UUID
    key: str
    document: Document


class AssignmentRow(_Row):
    item_key: str
    reviewer_key: str
    reserved_until: datetime | None
    done_at: datetime | None


class CommentRow(_Row):
    key: str
    run_id: uuid.UUID | None
    suite_id: uuid.UUID | None
    document: Document


class ItemIndexRow(_Row):
    key: str
    case_key: str
    arm_key: str
    repeat: int
    status: str


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


async def suite_by_key(session: AsyncSession, *, tenant_id: uuid.UUID, key: str) -> SuiteRow | None:
    row = await session.scalar(
        select(EvalSuite).where(EvalSuite.tenant_id == tenant_id, EvalSuite.key == key)
    )
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
) -> SuiteRow:
    row = EvalSuite(
        tenant_id=tenant_id, key=key, name=name, owner_key=owner_key, current_version=0,
        source_instance=source_instance,
    )  # fmt: skip
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return SuiteRow.model_validate(row)


async def add_suite_version(
    session: AsyncSession,
    suite: SuiteRow,
    *,
    version: int,
    document: Document,
    pins: Sequence[PinRow],
    scrub_status: str,
    created_by: str | None,
) -> None:
    """Store one suite version with its pins; the suite's latest moves to it if newer."""
    session.add(
        EvalSuiteVersion(
            suite_id=suite.id, version=version, tenant_id=suite.tenant_id, document=document,
            created_by=created_by,
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
    await session.execute(
        update(EvalSuite)
        .where(EvalSuite.id == suite.id)
        .values(
            current_version=func.greatest(EvalSuite.current_version, version),
            name=name if isinstance(name, str) and version >= suite.current_version else suite.name,
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
    session: AsyncSession, *, tenant_id: uuid.UUID, blob: BlobRow, content: bytes
) -> None:
    """Keep a blob; one already kept under the same sha256 stays as it is."""
    await session.execute(
        insert(EvalBlob)
        .values(tenant_id=tenant_id, content=content, **blob.model_dump())
        .on_conflict_do_nothing(index_elements=[EvalBlob.tenant_id, EvalBlob.sha256])
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


async def suites_using_blob(
    session: AsyncSession, *, tenant_id: uuid.UUID, sha256: str
) -> set[uuid.UUID]:
    """Suites whose cases or runs reference a blob."""
    cases = (
        select(EvalCase.suite_id)
        .join(EvalCaseRevision, EvalCaseRevision.case_id == EvalCase.id)
        .where(
            EvalCaseRevision.tenant_id == tenant_id, EvalCaseRevision.blob_shas.contains([sha256])
        )
    )
    runs = (
        select(EvalRun.suite_id)
        .join(EvalRunItem, EvalRunItem.run_id == EvalRun.id)
        .where(EvalRunItem.tenant_id == tenant_id, EvalRunItem.blob_shas.contains([sha256]))
    )
    return set(await session.scalars(cases.union(runs)))


async def exported_by(session: AsyncSession, *, tenant_id: uuid.UUID, sha256: str) -> set[str]:
    """Who exported the bundle stored as this blob."""
    rows = await session.scalars(
        select(EvalBundle.actor_key).where(
            EvalBundle.tenant_id == tenant_id, EvalBundle.blob_sha256 == sha256,
            EvalBundle.direction == "export",
        )
    )  # fmt: skip
    return set(rows)


async def record_bundle(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    direction: str,
    manifest: Document,
    actor_key: str,
    blob_sha256: str | None = None,
) -> None:
    session.add(
        EvalBundle(
            tenant_id=tenant_id, direction=direction, key=str(manifest["id"]), manifest=manifest,
            actor_key=actor_key, scrub_status=str(_get(manifest, "scrub", "status")),
            blob_sha256=blob_sha256,
        )
    )  # fmt: skip
    await session.flush()


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
    created_by: str | None,
) -> uuid.UUID:
    """Store a whole run as a bundle carries it; items come with the blobs they reference."""
    suite_pin = _get(document, "suite", "version")
    run = EvalRun(
        tenant_id=tenant_id, key=document["id"], suite_id=suite_id, suite_version=suite_pin,
        status=document["status"], origin=str(_get(document, "origin", "kind")), document=document,
        created_by=created_by,
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


# ---------- items, scores, queues and comments ----------


async def item_index(session: AsyncSession, *, run_id: uuid.UUID) -> list[ItemIndexRow]:
    """Every item of a run without its document, in a stable order."""
    rows = await session.execute(
        select(
            EvalRunItem.key, EvalRunItem.case_key, EvalRunItem.arm_key, EvalRunItem.repeat,
            EvalRunItem.status,
        )
        .where(EvalRunItem.run_id == run_id)
        .order_by(EvalRunItem.case_key, EvalRunItem.repeat, EvalRunItem.arm_key)
    )  # fmt: skip
    return [ItemIndexRow.model_validate(r, from_attributes=True) for r in rows]


async def items_by_key(
    session: AsyncSession, *, run_id: uuid.UUID, keys: Iterable[str]
) -> dict[str, ItemRow]:
    rows = await session.scalars(
        select(EvalRunItem).where(EvalRunItem.run_id == run_id, EvalRunItem.key.in_(list(keys)))
    )
    return {r.key: ItemRow.model_validate(r) for r in rows}


async def all_items(session: AsyncSession, *, run_id: uuid.UUID) -> list[ItemRow]:
    rows = await session.scalars(
        select(EvalRunItem).where(EvalRunItem.run_id == run_id).order_by(EvalRunItem.key)
    )
    return [ItemRow.model_validate(r) for r in rows]


async def scores_of_run(session: AsyncSession, *, run_id: uuid.UUID) -> list[Document]:
    rows = await session.scalars(
        select(EvalScore.document)
        .where(EvalScore.run_id == run_id)
        .order_by(EvalScore.created_at, EvalScore.key)
    )
    return list(rows)


async def put_scores(
    session: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID, scores: Sequence[Document]
) -> None:
    """Store scores; one with the id of an earlier score replaces it."""
    for score in scores:
        values = {
            "item_key": score.get("item_id"), "criterion": score["criterion"],
            "grader_kind": str(_get(score, "grader", "kind")), "document": score,
        }  # fmt: skip
        await session.execute(
            insert(EvalScore)
            .values(tenant_id=tenant_id, run_id=run_id, key=score["id"], **values)
            .on_conflict_do_update(constraint="uq_eval_scores_run_key", set_=values)
        )


async def queues_of_run(session: AsyncSession, *, run_id: uuid.UUID) -> list[QueueRow]:
    rows = await session.scalars(
        select(EvalReviewQueue)
        .where(EvalReviewQueue.run_id == run_id)
        .order_by(EvalReviewQueue.created_at, EvalReviewQueue.key)
    )
    return [QueueRow.model_validate(r, from_attributes=True) for r in rows]


async def queue_by_key(
    session: AsyncSession, *, run_id: uuid.UUID, key: str, lock: bool = False
) -> QueueRow | None:
    query = select(EvalReviewQueue).where(
        EvalReviewQueue.run_id == run_id, EvalReviewQueue.key == key
    )
    row = await session.scalar(query.with_for_update() if lock else query)
    return row and QueueRow.model_validate(row, from_attributes=True)


async def put_queue(
    session: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID, document: Document
) -> QueueRow:
    """Create a review queue, or replace the settings of the one with the same id."""
    row = await session.scalar(
        insert(EvalReviewQueue)
        .values(tenant_id=tenant_id, run_id=run_id, key=document["id"], document=document)
        .on_conflict_do_update(
            constraint="uq_eval_review_queues_run_key", set_={"document": document}
        )
        .returning(EvalReviewQueue)
    )
    return QueueRow.model_validate(row, from_attributes=True)


async def assignments_of(session: AsyncSession, *, queue_id: uuid.UUID) -> list[AssignmentRow]:
    rows = await session.scalars(
        select(EvalReviewAssignment).where(EvalReviewAssignment.queue_id == queue_id)
    )
    return [AssignmentRow.model_validate(r, from_attributes=True) for r in rows]


async def reserve_item(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    queue_id: uuid.UUID,
    item_key: str,
    reviewer_key: str,
    until: datetime,
) -> None:
    values = {"reserved_until": until}
    await session.execute(
        insert(EvalReviewAssignment)
        .values(
            queue_id=queue_id,
            item_key=item_key,
            reviewer_key=reviewer_key,
            tenant_id=tenant_id,
            **values,
        )
        .on_conflict_do_update(
            index_elements=["queue_id", "item_key", "reviewer_key"],
            set_=values,
            where=EvalReviewAssignment.done_at.is_(None),
        )
    )


async def finish_item(
    session: AsyncSession, *, queue_id: uuid.UUID, item_key: str, reviewer_key: str, at: datetime
) -> None:
    await session.execute(
        update(EvalReviewAssignment)
        .where(
            EvalReviewAssignment.queue_id == queue_id,
            EvalReviewAssignment.item_key == item_key,
            EvalReviewAssignment.reviewer_key == reviewer_key,
        )
        .values(done_at=at, reserved_until=None)
    )


async def add_comment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None,
    suite_id: uuid.UUID | None,
    document: Document,
) -> None:
    session.add(
        EvalComment(
            tenant_id=tenant_id, run_id=run_id, suite_id=suite_id, key=document["id"],
            document=document,
        )
    )  # fmt: skip
    await session.flush()


async def comments_of_run(session: AsyncSession, *, run_id: uuid.UUID) -> list[CommentRow]:
    rows = await session.scalars(
        select(EvalComment)
        .where(EvalComment.run_id == run_id)
        .order_by(EvalComment.created_at, EvalComment.key)
    )
    return [CommentRow.model_validate(r, from_attributes=True) for r in rows]


async def comment_by_key(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> CommentRow | None:
    row = await session.scalar(
        select(EvalComment).where(EvalComment.tenant_id == tenant_id, EvalComment.key == key)
    )
    return row and CommentRow.model_validate(row, from_attributes=True)


async def set_comment(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str, document: Document
) -> None:
    await session.execute(
        update(EvalComment)
        .where(EvalComment.tenant_id == tenant_id, EvalComment.key == key)
        .values(document=document)
    )

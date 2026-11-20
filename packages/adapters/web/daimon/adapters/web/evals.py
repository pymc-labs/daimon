"""`/api/evals/*`: suites, cases, arms, runs, items, blobs and bundles.

Every check lives in `daimon.core.evals.service`, the same one the MCP tools and the CLI
use; this module says who is asking and maps refusals to HTTP statuses.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated

from daimon.adapters.web.caller import WebCaller, groups_of, requires
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.dm_access import member_roles
from daimon.core.evals.access import Share, Subject, WorkspaceRole, platform_groups
from daimon.core.evals.compare import Comparison
from daimon.core.evals.format import Arm, Comment, JsonObject, to_document
from daimon.core.evals.review import (
    CommentDraft,
    QueueDraft,
    QueueView,
    ReviewService,
    ReviewTask,
    ScoreDraft,
)
from daimon.core.evals.scoring.client import MuxJudgeClient
from daimon.core.evals.scoring.service import JudgeOutcome, JudgeRequest, ScoringService
from daimon.core.evals.scoring.summary import RunSummary
from daimon.core.evals.service import (
    CaseCheck,
    CaseRef,
    CaseSummary,
    EvalForbidden,
    EvalNotFound,
    EvalRefused,
    ExportRequest,
    ImportReport,
    Page,
    RunItemSummary,
    SuiteDraft,
    SuiteSummary,
    eval_service,
)
from daimon.core.turn_hold import RequesterOrg
from daimon.core.web_identity import WebPrincipal
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

Caller = Annotated[WebCaller, Depends(requires("evals.use"))]


class QueueCreate(BaseModel):
    run_id: str
    queue: QueueDraft


class ScoreSubmit(BaseModel):
    task_id: str
    scores: list[ScoreDraft]


async def _call[T](work: Awaitable[T]) -> T:
    try:
        return await work
    except EvalRefused as exc:
        status = (
            404 if isinstance(exc, EvalNotFound) else 403 if isinstance(exc, EvalForbidden) else 400
        )
        detail = {"message": exc.message, "problems": list(exc.problems)}
        raise HTTPException(
            status_code=status, detail=detail if exc.problems else exc.message
        ) from None


def eval_subject(caller: WebCaller, principal: WebPrincipal) -> Subject:
    """The caller as the MCP tools see them: keyed by the platform id they act as, so a suite
    made here is theirs there too. Another organisation's people are visitors."""
    tenant_id = caller.person.tenant_id
    held = member_roles(groups_of(caller) | principal.groups)
    role: WorkspaceRole = "visitor" if principal.org is not RequesterOrg.HOST else caller.role
    return Subject(
        tenant_id=tenant_id,
        platform=principal.platform,
        key=principal.external_id,
        role=role,
        person_ids=frozenset({f"web:{caller.person.id}"}),
        groups=platform_groups(held, principal.external_id, tenant_id=tenant_id),
    )


def evals_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/evals")
    service = eval_service(runtime.sessionmaker, runtime.settings, runtime.anthropic)
    scoring = ScoringService(
        runtime.sessionmaker, runtime.settings, service, client=MuxJudgeClient(runtime.anthropic)
    )
    review = ReviewService(runtime.sessionmaker, service)

    async def subject(caller: WebCaller) -> Subject:
        async with runtime.sessionmaker() as db:
            return eval_subject(caller, await caller.principal(db))

    @router.get("/suites")
    async def list_suites(caller: Caller) -> list[SuiteSummary]:  # pyright: ignore[reportUnusedFunction]
        return await _call(service.suites(await subject(caller)))

    @router.post("/suites")
    async def create_suite(caller: Caller, draft: SuiteDraft) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        return to_document(await _call(service.save_suite(await subject(caller), draft, new=True)))

    @router.get("/suites/{suite_id}")
    async def get_suite(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, suite_id: str, version: int | None = None
    ) -> JsonObject:
        return to_document(await _call(service.suite(await subject(caller), suite_id, version)))

    @router.patch("/suites/{suite_id}")
    async def update_suite(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, suite_id: str, draft: SuiteDraft
    ) -> JsonObject:
        if draft.id != suite_id:
            raise HTTPException(status_code=400, detail="The suite id does not match the address.")
        return to_document(await _call(service.save_suite(await subject(caller), draft, new=False)))

    @router.put("/suites/{suite_id}/shares", status_code=204)
    async def share_suite(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, suite_id: str, shares: list[Share]
    ) -> None:
        await _call(service.share(await subject(caller), suite_id, shares))

    @router.get("/suites/{suite_id}/cases")
    async def list_cases(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, suite_id: str, version: int | None = None
    ) -> list[CaseSummary]:
        return await _call(service.cases(await subject(caller), suite_id, version))

    @router.get("/suites/{suite_id}/cases/{case_id}")
    async def get_case(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, suite_id: str, case_id: str, revision: int | None = None
    ) -> JsonObject:
        return await _call(service.case(await subject(caller), suite_id, case_id, revision))

    @router.put("/suites/{suite_id}/cases/{case_id}")
    async def put_case(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, suite_id: str, case_id: str, case: Annotated[JsonObject, Body()]
    ) -> CaseRef:
        if case.get("id") != case_id:
            raise HTTPException(status_code=400, detail="The case id does not match the address.")
        return await _call(service.put_case(await subject(caller), suite_id, case))

    @router.post("/cases/validate")
    async def validate_case(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, case: Annotated[JsonObject, Body()]
    ) -> CaseCheck:
        return await _call(service.check_case(await subject(caller), case))

    @router.get("/arms")
    async def list_arms(caller: Caller) -> list[JsonObject]:  # pyright: ignore[reportUnusedFunction]
        return [to_document(a) for a in await _call(service.arms(await subject(caller)))]

    @router.post("/arms")
    async def save_arm(caller: Caller, arm: Arm) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        return to_document(await _call(service.save_arm(await subject(caller), arm)))

    @router.get("/runs/{run_id}")
    async def get_run(caller: Caller, run_id: str) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        return to_document(await _call(service.run(await subject(caller), run_id)))

    @router.get("/runs/{run_id}/items")
    async def list_items(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, run_id: str, offset: int = 0, limit: int = 50, blind: bool = False
    ) -> list[RunItemSummary]:
        page = Page(offset=offset, limit=limit)
        return await _call(service.items(await subject(caller), run_id, page, blind=blind))

    @router.get("/runs/{run_id}/summary")
    async def run_summary(caller: Caller, run_id: str) -> RunSummary:  # pyright: ignore[reportUnusedFunction]
        return await _call(scoring.summary(await subject(caller), run_id))

    @router.get("/runs/{run_id}/compare")
    async def run_compare(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, run_id: str, arms: Annotated[list[str] | None, Query()] = None,
        blind: bool = False,
    ) -> Comparison:  # fmt: skip
        return await _call(scoring.compare(await subject(caller), run_id, arms=arms, blind=blind))

    @router.post("/runs/{run_id}/judge")
    async def judge_run(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, run_id: str, request: JudgeRequest
    ) -> JudgeOutcome:
        return await _call(scoring.judge(await subject(caller), run_id, request))

    @router.get("/queues")
    async def list_queues(caller: Caller, run_id: str) -> list[QueueView]:  # pyright: ignore[reportUnusedFunction]
        return await _call(review.queues(await subject(caller), run_id))

    @router.post("/queues")
    async def create_queue(caller: Caller, body: QueueCreate) -> QueueView:  # pyright: ignore[reportUnusedFunction]
        return await _call(review.create_queue(await subject(caller), body.run_id, body.queue))

    @router.get("/queues/{queue_ref}/next")
    async def next_task(caller: Caller, queue_ref: str) -> ReviewTask | None:  # pyright: ignore[reportUnusedFunction]
        return await _call(review.next(await subject(caller), queue_ref))

    @router.post("/scores", status_code=204)
    async def submit_scores(caller: Caller, body: ScoreSubmit) -> None:  # pyright: ignore[reportUnusedFunction]
        await _call(review.submit(await subject(caller), body.task_id, body.scores))

    @router.get("/comments")
    async def list_comments(caller: Caller, run_id: str) -> list[Comment]:  # pyright: ignore[reportUnusedFunction]
        return await _call(review.comments(await subject(caller), run_id))

    @router.post("/comments")
    async def add_comment(caller: Caller, draft: CommentDraft) -> Comment:  # pyright: ignore[reportUnusedFunction]
        return await _call(review.comment(await subject(caller), draft))

    @router.get("/items/{item_id}")
    async def get_item(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, item_id: str, blind: bool = False
    ) -> JsonObject:
        return to_document(await _call(service.item(await subject(caller), item_id, blind=blind)))

    @router.post("/bundles/export")
    async def export_bundle(  # pyright: ignore[reportUnusedFunction]
        caller: Caller,
        request: ExportRequest,
        passphrase: Annotated[str | None, Header(alias="X-Bundle-Passphrase")] = None,
    ) -> StreamingResponse:
        actor = await subject(caller)
        bundle = await _call(service.export_bundle(actor, request, passphrase=passphrase))
        headers = {
            "Content-Disposition": f'attachment; filename="{bundle.name}"',
            "Content-Length": str(bundle.size),
            "X-Content-Type-Options": "nosniff",
        }
        return StreamingResponse(bundle.chunks(), media_type=bundle.media_type, headers=headers)

    @router.post("/bundles/import")
    async def import_bundle(  # pyright: ignore[reportUnusedFunction]
        caller: Caller,
        request: Request,
        accept_unscrubbed: bool = False,
        passphrase: Annotated[str | None, Header(alias="X-Bundle-Passphrase")] = None,
    ) -> ImportReport:
        actor = await subject(caller)
        work = service.import_bundle(
            actor, request.stream(), accept_unscrubbed=accept_unscrubbed, passphrase=passphrase
        )
        return await _call(work)

    @router.get("/blobs/{sha256}")
    async def get_blob(caller: Caller, sha256: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        row, content = await _call(service.blob(await subject(caller), sha256))
        headers = {"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"}
        return Response(content, media_type=row.media_type, headers=headers)

    return router

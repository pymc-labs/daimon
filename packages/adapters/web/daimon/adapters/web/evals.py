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
from daimon.core.evals.access import Share, Subject, platform_groups
from daimon.core.evals.draft import auxiliary_completer
from daimon.core.evals.format import Arm, JsonObject, to_document
from daimon.core.evals.service import (
    CaseCheck,
    CaseRef,
    CaseSummary,
    DedupeReport,
    DraftResult,
    EvalForbidden,
    EvalNotFound,
    EvalRefused,
    EvalService,
    ExportRequest,
    ImportReport,
    Page,
    RunItemSummary,
    SuiteDraft,
    SuiteSummary,
)
from daimon.core.evals.web_thread import WebChatSource
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field


class FromThread(BaseModel):
    link: str


class DraftCases(BaseModel):
    case_ids: list[str]
    confirm: bool = False


class DedupeCases(BaseModel):
    keep: int | None = Field(default=None, ge=1)
    threshold: float = Field(default=0.8, gt=0, le=1)


Caller = Annotated[WebCaller, Depends(requires("evals.use"))]


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


def evals_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/evals")
    service = EvalService(runtime.sessionmaker, runtime.settings)

    async def subject(caller: WebCaller) -> Subject:
        async with runtime.sessionmaker() as db:
            principal = await caller.principal(db)
        tenant_id = caller.person.tenant_id
        held = member_roles(groups_of(caller) | principal.groups)
        return Subject(
            tenant_id=tenant_id,
            platform=principal.platform,
            key=f"web:{caller.person.id}",
            role=caller.role,
            person_ids=frozenset({principal.external_id}),
            groups=platform_groups(held, principal.external_id, tenant_id=tenant_id),
        )

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

    @router.post("/suites/{suite_id}/cases/from-thread")
    async def cases_from_thread(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, suite_id: str, body: FromThread
    ) -> list[CaseRef]:
        source = WebChatSource(
            runtime.sessionmaker, tenant_id=caller.person.tenant_id, person_id=caller.person.id
        )
        return await _call(
            service.cases_from_thread(await subject(caller), source, body.link, suite_id)
        )

    @router.post("/suites/{suite_id}/cases/draft")
    async def draft_cases(caller: Caller, suite_id: str, body: DraftCases) -> DraftResult:  # pyright: ignore[reportUnusedFunction]
        """Without `confirm`, only the cost estimate; with it, drafts and bills the workspace."""
        actor = await subject(caller)
        complete = None
        if body.confirm and runtime.anthropic is not None:
            complete = auxiliary_completer(
                runtime.anthropic,
                runtime.sessionmaker,
                tenant_id=actor.tenant_id,
                platform=actor.platform,
                user_id=next(iter(actor.person_ids), None),
                model=runtime.settings.evals.draft_model,
                markup=runtime.settings.billing.markup,
            )
        return await _call(
            service.draft_expectations(
                actor, suite_id, body.case_ids, complete=complete, confirm=body.confirm
            )
        )

    @router.post("/suites/{suite_id}/dedupe")
    async def dedupe_cases(caller: Caller, suite_id: str, body: DedupeCases) -> DedupeReport:  # pyright: ignore[reportUnusedFunction]
        return await _call(
            service.dedupe(
                await subject(caller), suite_id, threshold=body.threshold, keep=body.keep
            )
        )

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
        caller: Caller, run_id: str, offset: int = 0, limit: int = 50
    ) -> list[RunItemSummary]:
        page = Page(offset=offset, limit=limit)
        return await _call(service.items(await subject(caller), run_id, page))

    @router.get("/items/{item_id}")
    async def get_item(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, item_id: str, blind: bool = False
    ) -> JsonObject:
        actor = await subject(caller)
        found = service.item(actor, item_id, blind_for=actor.key if blind else None)
        return to_document(await _call(found))

    @router.post("/bundles/export")
    async def export_bundle(  # pyright: ignore[reportUnusedFunction]
        caller: Caller, request: ExportRequest
    ) -> JsonObject:
        return to_document(await _call(service.export_bundle(await subject(caller), request)))

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

"""Eval suites, cases, arms, run items and bundles, over `daimon.core.evals.service`.

The `evals` feature gates every tool here (`CAPABILITY_TOOLS`); the service checks it again
and decides who may do what.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from dataclasses import replace

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._dm_groups import slack_groups, slack_http, teams_groups
from daimon.adapters.mcp.tools._file_handles import staged_uploads
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core.dm_access import member_roles, resolve_groups
from daimon.core.evals.access import GroupLookup, Share, Subject, WorkspaceRole, platform_groups
from daimon.core.evals.compare import Comparison
from daimon.core.evals.format import Arm, Comment, JsonObject, to_document
from daimon.core.evals.review import CommentDraft, ReviewService, ReviewTask, ScoreDraft
from daimon.core.evals.scoring.service import JudgeOutcome, JudgeRequest, ScoringService
from daimon.core.evals.service import (
    CaseCheck,
    CaseRef,
    CaseSummary,
    EvalRefused,
    EvalService,
    ExportRequest,
    ImportReport,
    Page,
    RunItemSummary,
    SuiteDraft,
    SuiteSummary,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def _call[T](work: Awaitable[T]) -> T:
    try:
        return await work
    except EvalRefused as exc:
        raise ToolError("; ".join((exc.message, *exc.problems))) from None


def _groups(runtime: McpRuntime, auth: AuthIdentity, user_id: str) -> GroupLookup | None:
    """The caller's live groups on their platform; None where it cannot say."""
    if auth.platform == "discord":
        return platform_groups(
            member_roles(auth.verified_role_ids), user_id, tenant_id=auth.tenant_id
        )
    if auth.platform == "teams":
        return platform_groups(teams_groups(runtime), user_id, tenant_id=auth.tenant_id)
    team_id = auth.external_id
    if auth.platform != "slack" or team_id is None:
        return None

    async def slack(wanted: frozenset[str]) -> frozenset[str] | None:
        client = await slack_web_client(runtime, team_id=team_id)
        async with slack_http() as http:
            lookup = slack_groups(http, client, team_id=team_id)
            return await resolve_groups(lookup, user_id, wanted, tenant_id=auth.tenant_id)

    return slack


def eval_subject(runtime: McpRuntime, auth: AuthIdentity) -> Subject:
    """Who is calling: admins manage everything, clients of another organisation nothing."""
    user_id = auth.platform_user_id
    role: WorkspaceRole = (
        "admin" if auth.is_admin else "visitor" if auth.foreign_home_id else "member"
    )
    subject = Subject(
        tenant_id=auth.tenant_id,
        platform=auth.platform or "",
        key=user_id or f"account:{auth.account_id}",
        role=role,
    )
    groups = _groups(runtime, auth, user_id) if user_id else None
    return subject if groups is None else replace(subject, groups=groups)


def register_eval_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    service = EvalService(runtime.session_factory, runtime.settings)
    scoring = ScoringService(runtime.session_factory, runtime.settings)
    review = ReviewService(runtime.session_factory, runtime.settings)

    async def actor(ctx: Context) -> Subject:
        return eval_subject(runtime, await _auth(ctx))

    @mcp.tool
    async def eval_suites_list(ctx: Context) -> list[SuiteSummary]:  # pyright: ignore[reportUnusedFunction]
        """List the eval suites you can see, with your role on each."""
        return await _call(service.suites(await actor(ctx)))

    @mcp.tool
    async def eval_suite_get(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, suite_id: str, version: int | None = None
    ) -> JsonObject:
        """Read an eval suite: its cases, weights and defaults. No version reads the latest."""
        return to_document(await _call(service.suite(await actor(ctx), suite_id, version)))

    @mcp.tool
    async def eval_suite_create(ctx: Context, draft: SuiteDraft) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        """Create an eval suite with no cases; you own it. Add cases with eval_case_put."""
        return to_document(await _call(service.save_suite(await actor(ctx), draft, new=True)))

    @mcp.tool
    async def eval_suite_update(ctx: Context, draft: SuiteDraft) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        """Save the next version of an eval suite. Leave `cases` out to keep the cases it has."""
        return to_document(await _call(service.save_suite(await actor(ctx), draft, new=False)))

    @mcp.tool
    async def eval_suite_share(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, suite_id: str, shares: list[Share]
    ) -> str:
        """Replace who an eval suite is shared with: people or groups, as viewer, reviewer or
        editor. Only its owner or an admin can share; a suite that is not scrubbed cannot be."""
        await _call(service.share(await actor(ctx), suite_id, shares))
        return "Shared."

    @mcp.tool
    async def eval_cases_list(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, suite_id: str, version: int | None = None
    ) -> list[CaseSummary]:
        """List the cases of an eval suite version. No version lists the latest."""
        return await _call(service.cases(await actor(ctx), suite_id, version))

    @mcp.tool
    async def eval_case_get(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, suite_id: str, case_id: str, revision: int | None = None
    ) -> JsonObject:
        """Read one eval case as JSON. No revision reads the latest."""
        return await _call(service.case(await actor(ctx), suite_id, case_id, revision))

    @mcp.tool
    async def eval_case_put(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, suite_id: str, case: JsonObject
    ) -> CaseRef:
        """Add a case to an eval suite, or save its next revision. Check it first with
        eval_case_validate; the hash is filled in when missing."""
        return await _call(service.put_case(await actor(ctx), suite_id, case))

    @mcp.tool
    async def eval_case_validate(ctx: Context, case: JsonObject) -> CaseCheck:  # pyright: ignore[reportUnusedFunction]
        """Check an eval case against the format without saving it: problems and warnings."""
        return await _call(service.check_case(await actor(ctx), case))

    @mcp.tool
    async def eval_arms_list(ctx: Context) -> list[JsonObject]:  # pyright: ignore[reportUnusedFunction]
        """List the arms (agent setups to compare) in this workspace."""
        return [to_document(a) for a in await _call(service.arms(await actor(ctx)))]

    @mcp.tool
    async def eval_arm_create(ctx: Context, arm: Arm) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        """Create an arm, or change one you created. Admins can change any arm."""
        return to_document(await _call(service.save_arm(await actor(ctx), arm)))

    @mcp.tool
    async def eval_items_list(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, run_id: str, offset: int = 0, limit: int = 50
    ) -> list[RunItemSummary]:
        """List the items of an eval run, a page at a time (at most 500)."""
        page = Page(offset=offset, limit=limit)
        return await _call(service.items(await actor(ctx), run_id, page))

    @mcp.tool
    async def eval_item_get(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, item_id: str, blind: bool = False
    ) -> JsonObject:
        """Read one eval run item: its transcript, calls and outcome. `blind` hides the arm."""
        subject = await actor(ctx)
        found = service.item(subject, item_id, blind_for=subject.key if blind else None)
        return to_document(await _call(found))

    @mcp.tool
    async def eval_compare(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, run_id: str, arms: list[str] | None = None, blind: bool = False
    ) -> Comparison:
        """Compare the arms of an eval run side by side, case by case, with each item's replies
        and scores. `blind` hides arm and model names behind letters and shuffles the columns."""
        return await _call(scoring.compare(await actor(ctx), run_id, arms=arms, blind=blind))

    @mcp.tool
    async def eval_judge_run(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, run_id: str, request: JudgeRequest | None = None
    ) -> JudgeOutcome:
        """Estimate what judging an eval run with models would cost. Never runs the judges:
        a person approves the estimate and a spending ceiling on the evals page or in the CLI."""
        ask = (request or JudgeRequest()).model_copy(update={"approve_cap_usd": None})
        return await _call(scoring.judge(await actor(ctx), run_id, ask))

    @mcp.tool
    async def eval_review_next(ctx: Context, queue: str) -> ReviewTask | None:  # pyright: ignore[reportUnusedFunction]
        """Get your next review task from a queue (`run~queue`), or none when none is left.
        The task is yours for 30 minutes. Blind queues show letters, never arm or model names."""
        return await _call(review.next(await actor(ctx), queue))

    @mcp.tool
    async def eval_review_submit(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, task_id: str, scores: list[ScoreDraft]
    ) -> str:
        """Submit your scores for a review task: one per criterion the task asks for, naming
        the column (A, B...) each is about. Closes the task for you."""
        await _call(review.submit(await actor(ctx), task_id, scores, by="agent"))
        return "Saved."

    @mcp.tool
    async def eval_comment_add(ctx: Context, comment: CommentDraft) -> Comment:  # pyright: ignore[reportUnusedFunction]
        """Comment on an eval item or case, or from inside a review task by column letter."""
        return await _call(review.comment(await actor(ctx), comment))

    @mcp.tool
    async def eval_bundle_export(ctx: Context, request: ExportRequest) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        """Export suites and runs as a bundle. People are replaced by labels unless an owner
        or admin keeps them. Returns the bundle file's sha256 and size."""
        return to_document(await _call(service.export_bundle(await actor(ctx), request)))

    @mcp.tool
    async def eval_bundle_import(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        file_handle: str,
        accept_unscrubbed: bool = False,
        passphrase: str | None = None,
    ) -> ImportReport:
        """Import a bundle uploaded with create_file_upload_url. Only an owner or an admin
        can accept one that is not scrubbed. Give the passphrase of an encrypted bundle."""
        auth = await _auth(ctx)
        [(_, data)] = await staged_uploads(
            [file_handle], session_factory=runtime.session_factory, tenant_id=auth.tenant_id
        )

        async def chunks() -> AsyncIterator[bytes]:
            yield data

        work = service.import_bundle(
            eval_subject(runtime, auth),
            chunks(),
            accept_unscrubbed=accept_unscrubbed,
            passphrase=passphrase,
        )
        return await _call(work)

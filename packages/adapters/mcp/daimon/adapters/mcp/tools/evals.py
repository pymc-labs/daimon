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
from daimon.adapters.mcp.tools._eval_threads import thread_source
from daimon.adapters.mcp.tools._file_handles import staged_uploads
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core.dm_access import member_roles, resolve_groups
from daimon.core.evals.access import GroupLookup, Share, Subject, WorkspaceRole, platform_groups
from daimon.core.evals.draft import DraftEstimate
from daimon.core.evals.format import Arm, JsonObject, to_document
from daimon.core.evals.service import (
    CaseCheck,
    CaseRef,
    CaseSummary,
    DedupeReport,
    EvalRefused,
    ExportRequest,
    ImportReport,
    Page,
    RunItemSummary,
    SuiteDraft,
    SuiteSummary,
    eval_service,
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
    """Who is calling: admins manage everything, clients of another organisation nothing.
    External agent keys and held turns are never admins (`admin_for`)."""
    user_id = auth.platform_user_id
    role: WorkspaceRole = "admin" if auth.admin_for() else "visitor" if auth.client else "member"
    subject = Subject(
        tenant_id=auth.tenant_id,
        platform=auth.platform or "",
        key=user_id or f"account:{auth.account_id}",
        role=role,
    )
    groups = _groups(runtime, auth, user_id) if user_id else None
    return subject if groups is None else replace(subject, groups=groups)


def register_eval_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    service = eval_service(runtime.session_factory, runtime.settings, runtime.client)

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
        """Create an eval suite with no cases; you own it. Add cases with eval_case_put, then
        pin them with eval_suite_update."""
        return to_document(await _call(service.save_suite(await actor(ctx), draft, new=True)))

    @mcp.tool
    async def eval_suite_update(ctx: Context, draft: SuiteDraft) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        """Save the next version of an eval suite. `cases` pins case revisions (no revision:
        the latest); leave it out to keep the cases it pins."""
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
        """Add a case to an eval suite, or save its next revision; eval_suite_update pins it.
        Check it first with eval_case_validate; the hash is filled in when missing."""
        return await _call(service.put_case(await actor(ctx), suite_id, case))

    @mcp.tool
    async def eval_case_validate(ctx: Context, case: JsonObject) -> CaseCheck:  # pyright: ignore[reportUnusedFunction]
        """Check an eval case against the format without saving it: problems and warnings."""
        return await _call(service.check_case(await actor(ctx), case))

    @mcp.tool
    async def eval_case_from_thread(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, suite_id: str, link: str
    ) -> list[CaseRef]:
        """Make scrubbed eval cases from a thread you can read, given its link: one case per turn
        the agent answered. People, places and secrets are replaced before anything is saved."""
        auth = await _auth(ctx)
        source = thread_source(runtime, auth)
        return await _call(
            service.cases_from_thread(eval_subject(runtime, auth), source, link, suite_id)
        )

    @mcp.tool
    async def eval_cases_draft_estimate(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, suite_id: str, case_ids: list[str]
    ) -> DraftEstimate:
        """Estimate what it costs to draft expectations for cases with a model. Drafting itself
        needs a confirmation of the cost, given in the app or on the command line."""
        result = await _call(
            service.draft_expectations(
                await actor(ctx), suite_id, case_ids, complete=None, confirm=False
            )
        )
        return result.estimate

    @mcp.tool
    async def eval_cases_dedupe(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, suite_id: str, keep: int | None = None, threshold: float = 0.8
    ) -> DedupeReport:
        """Mark near-duplicate cases of a suite with a group. With `keep`, also drop all but that
        many of each group from the suite's next version."""
        return await _call(
            service.dedupe(await actor(ctx), suite_id, threshold=threshold, keep=keep)
        )

    @mcp.tool
    async def eval_arms_list(ctx: Context) -> list[JsonObject]:  # pyright: ignore[reportUnusedFunction]
        """List the arms (agent setups to compare) in this workspace."""
        return [to_document(a) for a in await _call(service.arms(await actor(ctx)))]

    @mcp.tool
    async def eval_arm_create(ctx: Context, arm: Arm) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        """Create an arm, or change one you created. Admins can change any arm. A live arm's
        agent must be one of this workspace's; its config hash is computed for you."""
        return to_document(await _call(service.save_arm(await actor(ctx), arm)))

    @mcp.tool
    async def eval_items_list(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, run_id: str, offset: int = 0, limit: int = 50, blind: bool = False
    ) -> list[RunItemSummary]:
        """List the items of an eval run, a page at a time (at most 500). `blind` names the
        arms by neutral labels; reviewers always see them so."""
        page = Page(offset=offset, limit=limit)
        return await _call(service.items(await actor(ctx), run_id, page, blind=blind))

    @mcp.tool
    async def eval_item_get(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, item_id: str, blind: bool = False
    ) -> JsonObject:
        """Read one eval run item: its transcript, calls and outcome. `blind` hides the arm
        and model; reviewers always get it so."""
        return to_document(await _call(service.item(await actor(ctx), item_id, blind=blind)))

    @mcp.tool
    async def eval_bundle_export(ctx: Context, request: ExportRequest) -> JsonObject:  # pyright: ignore[reportUnusedFunction]
        """Export suites and runs as a bundle. People are replaced by labels unless an admin
        keeps them. Returns a short-lived download link. Encrypted bundles are exported from the
        command line."""
        store = runtime.artifact_store
        if store is None or runtime.settings.artifacts is None:
            raise ToolError("Downloads are not set up here. Ask an admin or an owner.")
        bundle = await _call(service.export_bundle(await actor(ctx), request))
        stored = await store.upload_and_presign(
            key=f"evals/{(await _auth(ctx)).tenant_id}/exports/{bundle.name}",
            content=await bundle.read(),
            content_type=bundle.media_type,
            ttl_seconds=runtime.settings.artifacts.url_ttl_seconds,
        )
        return {
            "name": bundle.name, "size": bundle.size, "sha256": bundle.sha256, "url": stored.url,
            "expires_at": stored.expires_at.isoformat(),
        }  # fmt: skip

    @mcp.tool
    async def eval_bundle_import(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        file_handle: str,
        accept_unscrubbed: bool = False,
    ) -> ImportReport:
        """Import a bundle uploaded with create_file_upload_url. Only an owner or an admin
        can accept one that is not scrubbed. Encrypted bundles are imported from the command
        line."""
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
        )
        return await _call(work)

"""Workflow tools: read, draft, validate, publish and run workflows from chat.

Every tool is the same service call the web app makes (`daimon.core.workflows.service`), as
the person the chat belongs to. Drafts are edited against a revision, so a stale edit is
refused with the revision to read again. An agent key or an unattended turn can draft and
test but never publish; the service says so in words. In a Discord, Slack or Teams turn the
draft and run answer with cards (`daimon.core.workflows.chat_cards`): Publish and a live run
wait for the person's press, and a run card follows the run.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import get_session_turn_origin
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._own_card import post_own_card
from daimon.adapters.mcp.tools._web_url import tenant_links
from daimon.core.access_policy import DM_SCOPE_PREFIX, place_thread_id
from daimon.core.posted_controls import PostedCard
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.teams_threads import conversation_of
from daimon.core.web_links import TenantLinks, WebAt
from daimon.core.workflows import chat_cards, exchange, run_views, service, views
from daimon.core.workflows import edits as edits_module
from daimon.core.workflows.access import Who
from daimon.core.workflows.catalog import tenant_agents, tenant_skills
from daimon.core.workflows.chat_cards import ChatPlace
from daimon.core.workflows.service import Env, NewWorkflow, Patch, WorkflowError
from daimon.core.workflows.validate import Validation
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

Json = dict[str, Any]
CARD_PLATFORMS = frozenset({"discord", "slack", "teams"})
DRAFT_CARD = "The draft card is in the thread."
PUBLISH_CARD = "The draft card is in the thread. It goes live when the person presses Publish."
RUN_CARD = "The run card is in the thread. The run starts when the person presses Run."


def _env(runtime: McpRuntime, auth: AuthIdentity) -> Env:
    # Agent keys and unattended or foreign-sender turns act for a person, never as one.
    who = Who(
        auth.tenant_id,
        auth.account_id,
        admin=auth.admin_for(),
        external=auth.is_external_agent or auth.hold is not None or auth.client,
    )
    return Env(
        sm=runtime.session_factory,
        settings=runtime.settings,
        who=who,
        now=datetime.now(UTC),
        agents=tenant_agents(runtime.client, auth.tenant_id, runtime.session_factory),
        skills=tenant_skills(runtime.client, auth.tenant_id),
    )


@contextmanager
def _errors() -> Iterator[None]:
    """The service's plain refusals as the tool error the caller reads."""
    try:
        yield
    except service.Stale as stale:
        raise ToolError(
            f"The draft is at revision {stale.draft.revision} now. Read it with get_workflow "
            "and make the edit again from that revision. Nothing was changed."
        ) from stale
    except (WorkflowError, edits_module.EditError) as err:
        raise ToolError(str(err)) from err


def _problems(validation: Validation) -> Json:
    return {
        "errors": [p.model_dump(mode="json") for p in validation.errors],
        "warnings": [p.model_dump(mode="json") for p in validation.warnings],
        "grants": [g.model_dump(mode="json") for g in validation.grants],
    }


def _listed(w: views.WorkflowSummary) -> Json:
    return {
        "id": w.id,
        "name": w.name,
        "goal": w.goal,
        "status": w.status,
        "visibility": w.visibility,
        "owner": w.owner.name,
        "live_version": w.live_version,
        "draft_revision": w.draft_revision,
        "mine": w.mine,
        "failing": w.failing,
    }


def _run_line(run: run_views.RunSummary) -> Json:
    return {
        "id": run.id,
        "number": run.number,
        "workflow": run.workflow,
        "status": run.status,
        "waiting_reason": run.waiting_reason,
        "current_step": run.current_step,
        "label": run.label,
        "test": run.test,
        "version": run.version,
        "started_at": run.started_at,
        "cost_usd": run.cost_usd,
    }


async def _workflow_view(env: Env, workflow_id: uuid.UUID) -> Json:
    workflow = await service.get(env, workflow_id)
    draft = await service.get_draft(env, workflow_id)
    return {
        **_listed(workflow),
        "can_edit": workflow.can_edit,
        "can_run": workflow.can_run,
        "pending_publish_request": workflow.pending_request is not None,
        "draft": {
            "revision": draft.revision,
            "base_version": draft.base_version,
            "spec": draft.spec,
            **_problems(draft.validation),
        },
    }


def _links_of(runtime: McpRuntime, auth: AuthIdentity) -> WebAt:
    """The links at the turn's place: `tenant_links` reads it from the session's turn origin."""

    async def at(*_: object) -> TenantLinks | None:
        return await tenant_links(runtime, auth)

    return at


async def _chat_origin(runtime: McpRuntime, auth: AuthIdentity) -> TurnOriginRow | None:
    """The chat turn whose thread takes this call's cards, or None outside one."""
    if auth.platform not in CARD_PLATFORMS:
        return None
    return await get_session_turn_origin(runtime, auth)


async def _post(
    runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow, card: PostedCard
) -> ChatPlace:
    """Post `card` in the turn's thread; where it is, as the run card follower edits it."""
    message = await post_own_card(runtime, auth, origin=origin, card=card)
    if origin.platform == "teams":
        return ChatPlace("teams", conversation_of(origin.thread_id), message)
    if origin.platform == "discord":
        channel = place_thread_id(origin.thread_id) or origin.parent_channel_id
        return ChatPlace("discord", channel, message)
    thread = None if origin.thread_id.startswith(DM_SCOPE_PREFIX) else origin.thread_id
    return ChatPlace(origin.platform, origin.parent_channel_id, message, thread)


async def _post_draft(
    runtime: McpRuntime, env: Env, auth: AuthIdentity, origin: TurnOriginRow, workflow_id: uuid.UUID
) -> None:
    draft = await service.get_draft(env, workflow_id)
    web = await tenant_links(runtime, auth)
    await _post(runtime, auth, origin, chat_cards.draft_card(workflow_id, draft, web))


async def _list_workflows_impl(runtime: McpRuntime, auth: AuthIdentity) -> list[Json]:
    with _errors():
        return [_listed(w) for w in await service.list_workflows(_env(runtime, auth))]


async def _get_workflow_impl(
    runtime: McpRuntime, auth: AuthIdentity, workflow_id: uuid.UUID
) -> Json:
    with _errors():
        return await _workflow_view(_env(runtime, auth), workflow_id)


async def _create_workflow_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    name: str | None,
    template: str | None,
    spec: Json | None,
) -> Json:
    env = _env(runtime, auth)
    with _errors():
        if template is not None:
            body = NewWorkflow.model_validate({"from": "template", "template": template})
        elif spec is not None:
            body = NewWorkflow.model_validate(
                {"from": "import", "yaml": exchange.dump(spec, "yaml")}
            )
        else:
            body = NewWorkflow.model_validate({"from": "blank", "name": name})
        created = await service.create(env, body, runtime.settings.defaults_root)
        if name and (template is not None or spec is not None):
            created = await service.patch(env, uuid.UUID(created.id), Patch(name=name))
        return await _workflow_view(env, uuid.UUID(created.id))


async def _edit_workflow_draft_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    workflow_id: uuid.UUID,
    base_revision: int,
    edit_list: list[edits_module.Edit] | None,
    spec: Json | None,
) -> Json:
    if (edit_list is None) == (spec is None):
        raise ToolError("Give either `edits` or a whole `spec`, not both and not neither.")
    env = _env(runtime, auth)
    with _errors():
        if spec is None:
            current = await service.get_draft(env, workflow_id)
            if current.revision != base_revision:
                raise service.Stale(current)
            spec = edits_module.apply(current.spec, edit_list or [])
        saved = await service.save_draft(env, workflow_id, base_revision=base_revision, spec=spec)
    return {"revision": saved.revision, **_problems(saved.validation)}


async def _validate_workflow_impl(
    runtime: McpRuntime, auth: AuthIdentity, workflow_id: uuid.UUID, spec: Json | None
) -> Json:
    env = _env(runtime, auth)
    with _errors():
        origin = None if spec is not None else await _chat_origin(runtime, auth)
        if spec is None:
            spec = (await service.get_draft(env, workflow_id)).spec
        problems = _problems(await service.validate(env, workflow_id, spec))
        if origin is None:
            return problems
        await _post_draft(runtime, env, auth, origin, workflow_id)
    return {**problems, "card": DRAFT_CARD}


async def _publish_workflow_impl(
    runtime: McpRuntime, auth: AuthIdentity, workflow_id: uuid.UUID, note: str
) -> Json:
    env = _env(runtime, auth)
    with _errors():
        if (origin := await _chat_origin(runtime, auth)) is not None:
            # Publishing from chat is the person's press on the draft card.
            await _post_draft(runtime, env, auth, origin, workflow_id)
            return {"id": str(workflow_id), "status": "waiting", "message": PUBLISH_CARD}
        published = await service.publish(env, workflow_id, note, source="chat")
    return {"id": published.id, "status": published.status, "live_version": published.live_version}


async def _run_workflow_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    workflow_id: uuid.UUID,
    inputs: Json,
    test: bool,
) -> Json:
    env = _env(runtime, auth)
    with _errors():
        origin = await _chat_origin(runtime, auth)
        if origin is not None and not test:
            # A live run from chat starts on the person's press, with the inputs shown.
            workflow = await service.get(env, workflow_id)
            web = await tenant_links(runtime, auth)
            card = chat_cards.confirm_card(
                workflow_id, workflow.name, workflow.live_version, inputs, web
            )
            await _post(runtime, auth, origin, card)
            return {"id": str(workflow_id), "status": "waiting", "message": RUN_CARD}
        run = await service.start(env, workflow_id, inputs, test={} if test else None)
    shown = (
        None
        if origin is None
        else await chat_cards.run_card_of(
            env.sm, uuid.UUID(run.id), _links_of(runtime, auth), origin.parent_channel_id, None
        )
    )
    if origin is not None and shown is not None:
        place = await _post(runtime, auth, origin, shown)
        await chat_cards.show_on_card(env.sm, uuid.UUID(run.id), place)
    return _run_line(run)


async def _list_workflow_runs_impl(
    runtime: McpRuntime, auth: AuthIdentity, workflow_id: uuid.UUID | None, limit: int
) -> list[Json]:
    with _errors():
        runs = await service.list_runs(_env(runtime, auth), workflow_id, limit=limit)
    return [_run_line(r) for r in runs]


async def _get_workflow_run_impl(
    runtime: McpRuntime, auth: AuthIdentity, run_id: uuid.UUID
) -> Json:
    with _errors():
        run = await service.get_run(_env(runtime, auth), run_id)
    return {
        **_run_line(run),
        "outcome": run.outcome,
        "ended_at": run.ended_at,
        "failure": None if run.failure is None else run.failure.model_dump(mode="json"),
        "inputs": run.inputs,
        "steps": [
            {"key": s.key, "node": s.node, "status": s.status, "route": s.route, "reason": s.reason}
            for s in run.steps
        ],
    }


async def _clear_workflow_state_impl(
    runtime: McpRuntime, auth: AuthIdentity, workflow_id: uuid.UUID
) -> Json:
    env = _env(runtime, auth)
    with _errors():
        await service.ensure_on(env)
        cleared = await service.clear_state(env, workflow_id)
    return cleared.model_dump(mode="json")


async def _control_impl(run: run_views.RunSummary) -> Json:
    return _run_line(run)


def register_workflow_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_workflows(ctx: Context) -> list[Json]:  # pyright: ignore[reportUnusedFunction]
        """List the workflows this person can see: their own and those shared with the team."""
        return await _list_workflows_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def get_workflow(ctx: Context, workflow_id: uuid.UUID) -> Json:  # pyright: ignore[reportUnusedFunction]
        """Read a workflow: its draft spec and revision, its problems, and its live version.

        Read it before editing; edits name the revision they were made from.
        """
        return await _get_workflow_impl(runtime, await _auth(ctx), workflow_id)

    @mcp.tool
    async def create_workflow(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str | None = None,
        template: str | None = None,
        spec: Json | None = None,
    ) -> Json:
        """Create a workflow as a private draft owned by this person.

        Give a name for a blank one, a starter `template` id, or a whole `spec` (the
        daimon.workflow/v1 document). The result is the new workflow as get_workflow shows it.
        """
        return await _create_workflow_impl(
            runtime, await _auth(ctx), name=name, template=template, spec=spec
        )

    @mcp.tool
    async def edit_workflow_draft(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        workflow_id: uuid.UUID,
        base_revision: int,
        edits: list[edits_module.Edit] | None = None,
        spec: Json | None = None,
    ) -> Json:
        """Change a workflow's draft from the revision you read; nothing goes live.

        Give `edits` (add_node, update_node, remove_node, connect, set_trigger, set_inputs,
        set_start), applied in order, or a whole `spec`. A draft may have problems; they come
        back for you to fix. If the draft moved on, you are told its revision: read it again.
        """
        return await _edit_workflow_draft_impl(
            runtime,
            await _auth(ctx),
            workflow_id=workflow_id,
            base_revision=base_revision,
            edit_list=edits,
            spec=spec,
        )

    @mcp.tool
    async def validate_workflow(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, workflow_id: uuid.UUID, spec: Json | None = None
    ) -> Json:
        """Check a workflow's draft, or a spec you pass, and list errors, warnings and grants."""
        return await _validate_workflow_impl(runtime, await _auth(ctx), workflow_id, spec)

    @mcp.tool
    async def publish_workflow(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, workflow_id: uuid.UUID, note: Annotated[str, Field(max_length=500)] = ""
    ) -> Json:
        """Publish the draft as the next live version.

        Needs a person who may edit it, a draft without errors and no grants still to approve.
        """
        return await _publish_workflow_impl(runtime, await _auth(ctx), workflow_id, note)

    @mcp.tool
    async def run_workflow(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        workflow_id: uuid.UUID,
        inputs: Json | None = None,
        test: bool = False,
    ) -> Json:
        """Run the live version once, or with test=true the draft as a test run.

        A test run cannot write to real systems. Read the run with get_workflow_run.
        """
        return await _run_workflow_impl(
            runtime, await _auth(ctx), workflow_id=workflow_id, inputs=inputs or {}, test=test
        )

    @mcp.tool
    async def list_workflow_runs(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        workflow_id: uuid.UUID | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[Json]:
        """List recent runs, newest first: all you can see, or one workflow's."""
        return await _list_workflow_runs_impl(runtime, await _auth(ctx), workflow_id, limit)

    @mcp.tool
    async def get_workflow_run(ctx: Context, run_id: uuid.UUID) -> Json:  # pyright: ignore[reportUnusedFunction]
        """Read one run: its status, failure, inputs and each step's status."""
        return await _get_workflow_run_impl(runtime, await _auth(ctx), run_id)

    @mcp.tool
    async def cancel_workflow_run(ctx: Context, run_id: uuid.UUID) -> Json:  # pyright: ignore[reportUnusedFunction]
        """Stop a run that has not ended."""
        with _errors():
            return await _control_impl(
                await service.cancel(_env(runtime, await _auth(ctx)), run_id)
            )

    @mcp.tool
    async def retry_workflow_run(ctx: Context, run_id: uuid.UUID) -> Json:  # pyright: ignore[reportUnusedFunction]
        """Retry a failed or stopped run from where it stopped."""
        with _errors():
            return await _control_impl(
                await service.redrive(_env(runtime, await _auth(ctx)), run_id)
            )

    @mcp.tool
    async def fork_workflow_run(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        run_id: uuid.UUID,
        step: str | None = None,
        inputs: Json | None = None,
        version: str | int | None = None,
    ) -> Json:
        """Start a new run from an earlier one: with `step`, from that step on with the earlier
        steps' results reused; without, the whole run again. `inputs` replaces the inputs."""
        with _errors():
            run = await service.rerun(
                _env(runtime, await _auth(ctx)),
                run_id,
                step=step,
                inputs=inputs,
                version=version,
            )
        return _run_line(run)

    @mcp.tool
    async def set_workflow_paused(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, workflow_id: uuid.UUID, paused: bool
    ) -> Json:
        """Pause a live workflow so triggers stop starting runs, or resume it. Owner or admin."""
        with _errors():
            changed = await service.patch(
                _env(runtime, await _auth(ctx)), workflow_id, Patch(paused=paused)
            )
        return _listed(changed)

    @mcp.tool
    async def clear_workflow_state(ctx: Context, workflow_id: uuid.UUID) -> Json:  # pyright: ignore[reportUnusedFunction]
        """Clear what a workflow's runs saved between runs (`workflow.state`). Owner or editor."""
        return await _clear_workflow_state_impl(runtime, await _auth(ctx), workflow_id)


__all__ = ["register_workflow_tools"]

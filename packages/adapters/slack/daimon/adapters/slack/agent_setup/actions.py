"""Slack /agent-setup slash handler + block_action interactive handlers.

Shell module: all I/O lives here. Pure builders (views.py), reducers
(state.py), read-path (read.py), and write-path (write.py) are called
but never catch exceptions — failures propagate to the listener-boundary
catch in this module (S3 pattern).

Handler contract:
  handle_agent_setup_command(runtime, payload)
    Slash-command entry: open loading modal → background-fetch roster
    + is_admin → views.update with the L1 content view. On failure: update to
    the error view (UI-SPEC: no infinite spinner).

  handle_agent_setup_action(runtime, payload)
    Block-action dispatch for action_id matching ``agent_setup__*``. Ack-first
    discipline (app.py acks before spawning this handler). All I/O wrapped in
    the boundary catch.

Security — the gate follows the blast radius of what each branch touches,
never a blanket admin check ("hiding ≠ gating" principle: every refusal is
decided post-ack, server-side, never trusted from the rendered view):
  - Open to every caller: new, fork, edit (L1→L2 push), and the paste_secrets
    form push. Building an unscoped agent has no tenant-wide blast radius, and
    the paste-secrets form prompts for nothing at push time — the write it
    submits is refused at submission instead.
  - Shared-state gated via ``refuse_if_shared_and_not_admin`` in
    ``agent_setup.gate``: edit_repo_form and remove_secret. Repo bindings and
    env variables are per-agent attachments that never enter the agent spec, so
    they are exempt from the spec gate's absolutism about defaults-managed
    agents — an admin configuring the workspace's built-in agent is a supported
    first-run step. A non-admin is refused whenever the target is the built-in
    agent or one the workspace currently depends on, because a repo re-point or
    a secret deletion changes state every member's turns rely on.
    edit_repo_form is gated at the push because that form prompts for a GitHub
    personal access token and no credential should transit for a write that is
    going to be refused; the corresponding submissions are gated again in the
    submit module, which is the actual write boundary.
  - Field-conditional via the shared reachability gate in ``agent_setup.gate``:
    remove_skill, remove_mcp, edit_agent_form, add_skill, add_mcp. These touch
    the agent spec (system/model/skills/mcp_servers), so a non-admin is
    refused only when the target agent is currently reachable (a channel or
    workspace default) — an unreachable agent has no live gate to defend.
  - Admin-only unconditionally, unchanged: delete, the three scope:* branches,
    connect_mcp (mints a bearer token; token issuance stays outside what
    this phase opens). Delete carries one refusal above the admin check as
    well: the deployment's built-in agent is never deletable, admins included,
    and that refusal is posted as an ephemeral rather than raised, so the
    click does not look like a no-op.
  - JWT values for connect-mcp: never logged, last4/presence only.
  - Stale agent: re-fetch tolerates missing agent → no write, re-render L1 with
    warning notice.
  - Content-fetch failure → build_error_view, never an infinite spinner.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import uuid
from typing import Any

import anthropic
import jwt as pyjwt
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup import gate
from daimon.adapters.slack.agent_setup.read import (
    load_agent_channel_ids,
    load_scope_hint,
    load_section_data,
    load_tenant_roster,
)
from daimon.adapters.slack.agent_setup.state import (
    AgentSetupState,
    decode_private_metadata,
)
from daimon.adapters.slack.agent_setup.views import (
    build_agent_section,
    build_error_view,
    build_l1_view,
    build_l2_view,
    build_l3_add_mcp_form,
    build_l3_add_skill_form,
    build_l3_edit_agent_form,
    build_l3_edit_repo_form,
    build_l3_fork_agent_form,
    build_l3_new_agent_form,
    build_l3_paste_secrets_form,
    build_loading_view,
    build_mcps_section,
    build_repo_auth_section,
    build_secrets_section,
    build_skills_section,
)
from daimon.adapters.slack.agent_setup.write import (
    delete_agent,
    do_propagate,
    do_unpropagate,
    replace_agent_resources_for_panel,
)
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    list_agents_by_tenant,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token
from daimon.core.observability import capture_exception_with_scope
from daimon.core.scope import (
    ChannelScopeRef,
    TenantScopeRef,
)
from daimon.core.specs import AgentSpec
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from slack_sdk.errors import SlackApiError
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

# Refusal copy for Delete against the deployment's built-in agent. Worded in
# the shape ``agent_setup.gate`` uses for its own seeded-agent refusal: what is
# refused, that admin rights do not change it, and the fork path out.
_BUILTIN_AGENT_DELETE_MESSAGE = (
    ":lock: This is the workspace's built-in agent, so it cannot be deleted "
    "from here — not even by an admin. Fork it to get a copy you own, then "
    "delete the fork."
)


# ---------------------------------------------------------------------------
# Request ID for error views
# ---------------------------------------------------------------------------


def _new_request_id() -> str:
    """Generate a short opaque request ID for error cross-referencing."""
    return str(uuid.uuid4())[:8]


# ---------------------------------------------------------------------------
# Stale-agent L1 re-render helper
# ---------------------------------------------------------------------------


async def _render_stale_l1(
    client: Any,
    *,
    view_id: str,
    team_id: str,
    channel_id: str,
    agent_name: str,
    is_admin: bool,
    runtime: SlackRuntime,
    tenant_id: uuid.UUID,
) -> None:
    """Re-render L1 with a stale-agent warning notice (UI-SPEC stale handling).

    Attempt no write; just refresh the roster and add the :warning: context block
    as the scope_hint so the user knows what happened.
    """
    async with runtime.sessionmaker() as session:
        entries, over_cap = await load_tenant_roster(
            session, runtime.anthropic, tenant_id=tenant_id
        )
    stale_hint = (
        f":warning: `{agent_name}` is no longer available "
        "— it may have been deleted. Showing the current roster."
    )
    state = AgentSetupState(rows=entries, over_cap_count=over_cap)
    view = build_l1_view(
        state,
        is_admin=is_admin,
        team_id=team_id,
        channel_id=channel_id,
        selected_agent_name=None,
        scope_hint=stale_hint,
    )
    await client.views_update(  # pyright: ignore[reportUnknownMemberType]
        view_id=view_id,
        view=view,
    )


# ---------------------------------------------------------------------------
# Account resolution for actor_account_id
# ---------------------------------------------------------------------------


async def _resolve_actor_account_id(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
) -> uuid.UUID:
    """Resolve the actor's account_id from their Slack user ID.

    Uses ``get_or_create_platform_principal`` to find or mint the account.
    Returns a deterministic account UUID for attribution/audit.
    """
    from daimon.core.stores.identity import get_or_create_platform_principal

    async with runtime.sessionmaker() as session, session.begin():
        principal = await get_or_create_platform_principal(
            session,
            platform="slack",
            external_id=user_id,
            tenant_id=tenant_id,
        )
    return principal.account_id


# ---------------------------------------------------------------------------
# Render MCP config snippet (port of Discord mcp_access.py:render_mcp_config)
# ---------------------------------------------------------------------------


def _render_mcp_config_ephemeral(*, agent_name: str, public_url: str, jwt: str) -> str:
    """Build the copyable MCP config message for a per-agent MCP token.

    Follows the UI-SPEC "Connect via MCP" ephemeral card format.
    The JWT value is never logged — this function returns it in the message
    body only (T-83-11: value shown once in ephemeral only, never in logs).
    """
    key_name = f"daimon-{agent_name}"
    config = {
        key_name: {
            "url": public_url,
            "headers": {"Authorization": f"Bearer {jwt}"},
        }
    }
    mcp_json_block = json.dumps(config, indent=2)
    return (
        f":link: *Connect via MCP — {agent_name}*\n\n"
        "Token (shown once):\n"
        f"`{jwt}`\n\n"
        "Add to your MCP config:\n"
        f"```json\n{mcp_json_block}\n```\n\n"
        f"_This token grants access to {agent_name} only. "
        "Revoke it from /agent-setup → MCPs._"
    )


# ---------------------------------------------------------------------------
# Section blocks builder (helper for tab dispatch)
# ---------------------------------------------------------------------------


def _build_section_blocks(
    *,
    section: str,
    section_data: object,
    agent_name: str,
    is_admin: bool,
    can_edit_spec: bool,
) -> list[dict[str, Any]]:
    """Build the per-section blocks from ``load_section_data`` output.

    Handles type mapping from the heterogeneous ``load_section_data`` return
    to the specific builder signatures. ``can_edit_spec`` gates the
    spec-touching sections (agent/skills/mcps); repo_auth and secrets are
    per-agent attachments and render their mutation controls unconditionally.
    """
    if section == "agent":
        info: dict[str, Any] = dict(section_data) if isinstance(section_data, dict) else {}  # type: ignore[arg-type]
        return build_agent_section(
            agent_name=agent_name,
            model_id=str(info.get("model_id") or ""),
            system_prompt=str(info.get("system_prompt") or ""),
            can_edit_spec=can_edit_spec,
        )
    elif section == "repo_auth":
        return build_repo_auth_section(
            repo=None,
            pat_last4=None,
        )
    elif section == "skills":
        names: list[str] = list(section_data) if isinstance(section_data, list) else []  # type: ignore[arg-type]
        return build_skills_section(
            skill_names=names,
            sync_pending=False,
            can_edit_spec=can_edit_spec,
        )
    elif section == "mcps":
        mcp_names: list[str] = list(section_data) if isinstance(section_data, list) else []  # type: ignore[arg-type]
        mcps = [{"name": n, "url": ""} for n in mcp_names]
        return build_mcps_section(mcps=mcps, can_edit_spec=can_edit_spec, is_admin=is_admin)
    elif section == "secrets":
        secret_names: list[str] = list(section_data) if isinstance(section_data, list) else []  # type: ignore[arg-type]
        return build_secrets_section(
            agent_name=agent_name,
            secret_names=secret_names,
        )
    else:
        return []


# ---------------------------------------------------------------------------
# Spec-editability for un-gated renders (branches that don't call the shared
# reachability gate but still need to know whether to render a section's
# mutation controls -- e.g. `edit` and `remove_secret`, which are open to
# every member and re-render a spec-touching section)
# ---------------------------------------------------------------------------


async def _compute_can_edit_spec(
    runtime: SlackRuntime,
    *,
    is_admin: bool,
    tenant_id: uuid.UUID,
    agent_name: str,
) -> bool:
    """Whether the caller may change this agent's approved configuration.

    True immediately for an admin (no DB read). Otherwise a single
    reachability read decides: an unscoped agent has no live gate to
    defend, so a member may still change its spec.
    """
    if is_admin:
        return True
    async with runtime.sessionmaker() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=tenant_id,
            agent_name=agent_name,
            default=runtime.deployment_default,
        )
    return not reachable


# ---------------------------------------------------------------------------
# Slash-command entry
# ---------------------------------------------------------------------------


async def handle_agent_setup_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Slash command handler for /agent-setup (loading-modal pattern).

    Immediately opens a "Loading…" modal with the fresh trigger_id (beats the
    ~3s expiry), then background-fetches the roster + resolves is_admin, and
    updates the modal in place with the L1 content view.

    On fetch failure: updates the modal to the error view — never leaves the
    spinner (loading-modal pattern).

    Args:
        runtime: Injected SlackRuntime (sessionmaker, anthropic, settings).
        payload: Slash-command payload from the Socket Mode envelope.
    """
    team_id: str = payload.get("team_id") or ""
    trigger_id: str = payload.get("trigger_id") or ""
    channel_id: str = payload.get("channel_id") or ""
    user_id: str = payload.get("user_id") or ""

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.agent_setup_command.no_token", team_id=team_id)
        return

    view_id: str = ""
    try:
        # Open loading modal immediately — must beat the ~3s trigger_id TTL.
        resp = await client.views_open(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=build_loading_view(team_id=team_id, channel_id=channel_id),
        )
        view_id = resp["view"]["id"]  # pyright: ignore[reportUnknownVariableType, reportAssignmentType, reportOptionalSubscript, reportUnknownArgumentType]

        # Slow path (off the 3s window): resolve tenant + is_admin + fetch roster.
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        is_admin = await resolve_is_admin(client, user_id=user_id)

        async with runtime.sessionmaker() as session:
            entries, over_cap = await load_tenant_roster(
                session, runtime.anthropic, tenant_id=tenant_id
            )

        state = AgentSetupState(rows=entries, over_cap_count=over_cap)
        scope_hint = "_(no default set for this agent)_"

        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,  # pyright: ignore[reportUnknownArgumentType]
            view=build_l1_view(
                state,
                is_admin=is_admin,
                team_id=team_id,
                channel_id=channel_id,
                selected_agent_name=None,
                scope_hint=scope_hint,
            ),
        )

    except (DaimonError, anthropic.APIError, SlackApiError, InvalidToken, SQLAlchemyError) as exc:
        log.error("slack.agent_setup_command_failed", team_id=team_id, exc_info=exc)
        capture_exception_with_scope(exc)
        # No infinite spinner — update to error view on failure.
        if view_id:
            request_id = _new_request_id()
            # Swallow secondary failure — best-effort error render.
            with contextlib.suppress(Exception):
                await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                    view_id=view_id,  # pyright: ignore[reportUnknownArgumentType]
                    view=build_error_view(request_id=request_id),
                )


# ---------------------------------------------------------------------------
# Block-action dispatcher
# ---------------------------------------------------------------------------


async def handle_agent_setup_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Block-action handler for all agent_setup__* action_ids.

    Dispatches on ``actions[0]["action_id"]`` after extracting common context
    from the payload (team_id, user_id, view_id, private_metadata).

    Ack-first discipline: this handler is called AFTER app.py has already sent
    the empty block_actions ack. I/O is done directly here; no second ack.

    Mutation branches (scope, delete, remove-*) re-resolve is_admin server-side
    post-ack and refuse on False — hiding ≠ gating.

    Args:
        runtime: Injected SlackRuntime.
        payload: block_actions payload from the Socket Mode interactive envelope.
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    team_id: str = team_info.get("id") or payload.get("team_id") or ""  # type: ignore[assignment]
    user_info: dict[str, Any] = payload.get("user") or {}
    user_id: str = user_info.get("id") or ""
    actions: list[dict[str, Any]] = payload.get("actions") or []
    view_info: dict[str, Any] = payload.get("view") or {}
    view_id: str = view_info.get("id") or ""
    raw_meta: str = view_info.get("private_metadata") or ""
    meta = decode_private_metadata(raw_meta)

    team_id = team_id or meta.get("team_id") or ""
    channel_id: str = meta.get("channel_id") or ""
    selected_agent_name: str | None = meta.get("selected_agent_name") or meta.get("agent_name")
    active_section: str = meta.get("active_section") or "agent"

    if not actions:
        return

    action = actions[0]
    action_id: str = action.get("action_id") or ""

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.agent_setup_action.no_token", team_id=team_id, action_id=action_id)
        return

    try:
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

        # -----------------------------------------------------------------------
        # Roster select (read-only — no admin gate needed)
        # -----------------------------------------------------------------------
        if action_id == "agent_setup__roster_select":
            chosen: str = action.get("selected_option", {}).get("value") or ""
            if not chosen:
                return

            async with runtime.sessionmaker() as session:
                entries, over_cap = await load_tenant_roster(
                    session, runtime.anthropic, tenant_id=tenant_id
                )
                scope_hint = await load_scope_hint(
                    session,
                    tenant_id=tenant_id,
                    agent_name=chosen,
                    channel_id=channel_id,
                )

            is_admin = await resolve_is_admin(client, user_id=user_id)

            # Stale check — if the chosen agent is not in the fresh roster, re-render warning.
            agent_exists = any(e.agent_name == chosen for e in entries)
            if not agent_exists:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=chosen,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            state = AgentSetupState(rows=entries, over_cap_count=over_cap)
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l1_view(
                    state,
                    is_admin=is_admin,
                    team_id=team_id,
                    channel_id=channel_id,
                    selected_agent_name=chosen,
                    scope_hint=scope_hint,
                ),
            )

        # -----------------------------------------------------------------------
        # Section tab swap — L2 in-place via views.update (NEVER views.push)
        # Structural Guarantee #2: tabs swap in-place, never push
        # -----------------------------------------------------------------------
        elif action_id.startswith("agent_setup__tab:"):
            section = action_id.removeprefix("agent_setup__tab:")
            agent_name_for_tab = selected_agent_name or ""
            if not agent_name_for_tab:
                return

            is_admin = await resolve_is_admin(client, user_id=user_id)

            # Stale check before fetching section data.
            agents_check = await list_agents_by_tenant(runtime.anthropic, tenant_id=tenant_id)
            agent_exists = any(
                a.metadata.get(MA_METADATA_KEY_NAME) == agent_name_for_tab for a in agents_check
            )
            if not agent_exists:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_tab,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            can_edit_spec = await _compute_can_edit_spec(
                runtime, is_admin=is_admin, tenant_id=tenant_id, agent_name=agent_name_for_tab
            )

            async with runtime.sessionmaker() as session:
                section_data = await load_section_data(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    agent_name=agent_name_for_tab,
                    section=section,
                )

            section_blocks = _build_section_blocks(
                section=section,
                section_data=section_data,
                agent_name=agent_name_for_tab,
                is_admin=is_admin,
                can_edit_spec=can_edit_spec,
            )

            # views.update — NEVER views.push (3-level cap, Structural Guarantee #2)
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l2_view(
                    agent_name=agent_name_for_tab,
                    active_section=section,
                    team_id=team_id,
                    channel_id=channel_id,
                    is_admin=is_admin,
                    can_edit_spec=can_edit_spec,
                    section_blocks=section_blocks,
                ),
            )

        # -----------------------------------------------------------------------
        # Edit button — push to L2 (the ONLY transition that pushes to L2)
        # Open to every member: building/configuring an unscoped
        # agent needs no admin. can_edit_spec (admin, or the agent is not
        # currently reachable) decides whether the agent-spec controls render.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__edit":
            agent_name_for_edit = selected_agent_name or ""
            if not agent_name_for_edit:
                return

            is_admin = await resolve_is_admin(client, user_id=user_id)

            # Stale check.
            agents_check = await list_agents_by_tenant(runtime.anthropic, tenant_id=tenant_id)
            agent_exists = any(
                a.metadata.get(MA_METADATA_KEY_NAME) == agent_name_for_edit for a in agents_check
            )
            if not agent_exists:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_edit,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            can_edit_spec = await _compute_can_edit_spec(
                runtime, is_admin=is_admin, tenant_id=tenant_id, agent_name=agent_name_for_edit
            )

            async with runtime.sessionmaker() as session:
                agent_data = await load_section_data(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    agent_name=agent_name_for_edit,
                    section="agent",
                )

            agent_info: dict[str, Any] = dict(agent_data) if isinstance(agent_data, dict) else {}  # type: ignore[arg-type]
            section_blocks = build_agent_section(
                agent_name=agent_name_for_edit,
                model_id=str(agent_info.get("model_id") or ""),
                system_prompt=str(agent_info.get("system_prompt") or ""),
                can_edit_spec=can_edit_spec,
            )

            # views.push — Edit is the ONLY action that pushes to L2.
            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_l2_view(
                    agent_name=agent_name_for_edit,
                    active_section="agent",
                    team_id=team_id,
                    channel_id=channel_id,
                    is_admin=is_admin,
                    can_edit_spec=can_edit_spec,
                    section_blocks=section_blocks,
                ),
            )

        # -----------------------------------------------------------------------
        # Delete (archive) — MUTATION: re-resolve is_admin (T-83-10)
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__delete":
            is_admin = await resolve_is_admin(client, user_id=user_id)
            if not is_admin:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=":x: You no longer have permission to change agent setup.",
                )
                return

            target_name = selected_agent_name or ""
            if not target_name:
                return

            # Stale guard.
            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=target_name
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=target_name,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            # Built-in agent: a policy refusal, not a failure. ``delete_agent``
            # raises for this case and stays the server-side backstop, but a
            # raise reaches only the boundary catch at the bottom of this
            # function — which logs and reports to Sentry without touching
            # Slack, so the click would look like nothing happened and the
            # admin would click again. Read the marker off the agent already
            # fetched for the stale guard (no extra round trip) and refuse in
            # the shape ``agent_setup.gate`` uses: ephemeral, then return. No
            # repaint — the agent still exists, so the panel on screen is still
            # accurate, and the non-admin refusal above returns the same way.
            if ma_agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
                log.info(
                    "slack.agent_setup.delete_refused_builtin",
                    team_id=team_id,
                    agent_name=target_name,
                )
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=_BUILTIN_AGENT_DELETE_MESSAGE,
                )
                return

            await delete_agent(runtime, tenant_id=tenant_id, name=target_name)
            log.info("slack.agent_setup.agent_deleted", agent_name=target_name)

            async with runtime.sessionmaker() as session:
                entries, over_cap = await load_tenant_roster(
                    session, runtime.anthropic, tenant_id=tenant_id
                )
            state = AgentSetupState(rows=entries, over_cap_count=over_cap)
            delete_hint = (
                f":wastebasket: `{target_name}` deleted. _(Archived, not permanently erased.)_"
            )
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l1_view(
                    state,
                    is_admin=is_admin,
                    team_id=team_id,
                    channel_id=channel_id,
                    selected_agent_name=None,
                    scope_hint=delete_hint,
                ),
            )

        # -----------------------------------------------------------------------
        # Scope: whole workspace — MUTATION: re-resolve is_admin (T-83-10)
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__scope:workspace":
            is_admin = await resolve_is_admin(client, user_id=user_id)
            if not is_admin:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=":x: You no longer have permission to change agent setup.",
                )
                return

            if not selected_agent_name:
                return

            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=selected_agent_name
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=selected_agent_name,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            actor_account_id = await _resolve_actor_account_id(
                runtime, tenant_id=tenant_id, user_id=user_id
            )
            async with runtime.sessionmaker() as session, session.begin():
                await do_propagate(
                    session,
                    scope=TenantScopeRef(tenant_id=tenant_id),
                    tenant_id=tenant_id,
                    agent_name=selected_agent_name,
                    actor_account_id=actor_account_id,
                )

            async with runtime.sessionmaker() as session:
                entries, over_cap = await load_tenant_roster(
                    session, runtime.anthropic, tenant_id=tenant_id
                )
                scope_hint = await load_scope_hint(
                    session,
                    tenant_id=tenant_id,
                    agent_name=selected_agent_name,
                    channel_id=channel_id,
                )

            state = AgentSetupState(rows=entries, over_cap_count=over_cap)
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l1_view(
                    state,
                    is_admin=is_admin,
                    team_id=team_id,
                    channel_id=channel_id,
                    selected_agent_name=selected_agent_name,
                    scope_hint=scope_hint,
                ),
            )

        # -----------------------------------------------------------------------
        # Scope: this channel — MUTATION: re-resolve is_admin (T-83-10)
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__scope:channel":
            is_admin = await resolve_is_admin(client, user_id=user_id)
            if not is_admin:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=":x: You no longer have permission to change agent setup.",
                )
                return

            if not selected_agent_name:
                return

            # channels_select delivers the chosen channel in action["selected_channel"].
            selected_channel: str = action.get("selected_channel") or channel_id

            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=selected_agent_name
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=selected_agent_name,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            actor_account_id = await _resolve_actor_account_id(
                runtime, tenant_id=tenant_id, user_id=user_id
            )
            async with runtime.sessionmaker() as session, session.begin():
                await do_propagate(
                    session,
                    scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=selected_channel),
                    tenant_id=tenant_id,
                    agent_name=selected_agent_name,
                    actor_account_id=actor_account_id,
                )

            async with runtime.sessionmaker() as session:
                entries, over_cap = await load_tenant_roster(
                    session, runtime.anthropic, tenant_id=tenant_id
                )
                scope_hint = await load_scope_hint(
                    session,
                    tenant_id=tenant_id,
                    agent_name=selected_agent_name,
                    channel_id=selected_channel,
                )

            state = AgentSetupState(rows=entries, over_cap_count=over_cap)
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l1_view(
                    state,
                    is_admin=is_admin,
                    team_id=team_id,
                    channel_id=channel_id,
                    selected_agent_name=selected_agent_name,
                    scope_hint=scope_hint,
                ),
            )

        # -----------------------------------------------------------------------
        # Scope: clear — MUTATION: re-resolve is_admin (T-83-10)
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__scope:clear":
            is_admin = await resolve_is_admin(client, user_id=user_id)
            if not is_admin:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=":x: You no longer have permission to change agent setup.",
                )
                return

            if not selected_agent_name:
                return

            actor_account_id = await _resolve_actor_account_id(
                runtime, tenant_id=tenant_id, user_id=user_id
            )
            # Clear all channel defaults for this agent across every channel where
            # it is propagated (scan via store — do NOT hard-code the invoking channel).
            async with runtime.sessionmaker() as session:
                agent_channel_ids = await load_agent_channel_ids(
                    session,
                    tenant_id=tenant_id,
                    agent_name=selected_agent_name,
                )
            for ch_id in agent_channel_ids:
                async with runtime.sessionmaker() as session, session.begin():
                    await do_unpropagate(
                        session,
                        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=ch_id),
                        actor_account_id=actor_account_id,
                    )
            # Clear the workspace (tenant) scope unconditionally.
            async with runtime.sessionmaker() as session, session.begin():
                await do_unpropagate(
                    session,
                    scope=TenantScopeRef(tenant_id=tenant_id),
                    actor_account_id=actor_account_id,
                )

            async with runtime.sessionmaker() as session:
                entries, over_cap = await load_tenant_roster(
                    session, runtime.anthropic, tenant_id=tenant_id
                )
                scope_hint = await load_scope_hint(
                    session,
                    tenant_id=tenant_id,
                    agent_name=selected_agent_name,
                    channel_id=channel_id,
                )

            state = AgentSetupState(rows=entries, over_cap_count=over_cap)
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l1_view(
                    state,
                    is_admin=is_admin,
                    team_id=team_id,
                    channel_id=channel_id,
                    selected_agent_name=selected_agent_name,
                    scope_hint=scope_hint,
                ),
            )

        # -----------------------------------------------------------------------
        # Remove skill — field-conditional: skills are part of the agent
        # spec, so this refuses a non-admin only when the target agent is
        # currently reachable. Re-resolves via the shared gate post-ack.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__remove_skill":
            is_admin = await resolve_is_admin(client, user_id=user_id)

            skill_to_remove: str = action.get("selected_option", {}).get("value") or ""
            if not skill_to_remove or skill_to_remove == "__none__":
                return

            agent_name_for_remove = selected_agent_name or ""
            if not agent_name_for_remove:
                return

            if await gate.refuse_if_reachable_and_not_admin(
                runtime,
                client,
                tenant_id=tenant_id,
                agent_name=agent_name_for_remove,
                channel_id=channel_id,
                user_id=user_id,
            ):
                return

            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_remove
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_remove,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            ma_agent_full = await runtime.anthropic.beta.agents.retrieve(ma_agent.id)
            current_skills = [sk.skill_id for sk in (ma_agent_full.skills or [])]
            new_skills = [s for s in current_skills if s != skill_to_remove]
            spec = AgentSpec.model_validate(
                {
                    "name": agent_name_for_remove,
                    "model": ma_agent_full.model.id,
                    "skills": [{"id": s} for s in new_skills],
                    "mcp_servers": [
                        {"name": m.name, "url": m.url} for m in (ma_agent_full.mcp_servers or [])
                    ],
                }
            )
            await replace_agent_resources_for_panel(runtime, tenant_id=tenant_id, spec=spec)

            async with runtime.sessionmaker() as session:
                updated_data = await load_section_data(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    agent_name=agent_name_for_remove,
                    section="skills",
                )
            skill_names: list[str] = list(updated_data) if isinstance(updated_data, list) else []  # type: ignore[arg-type]
            # Reaching here means the gate above proceeded: either admin, or
            # the target was unreachable -- exactly can_edit_spec's definition.
            section_blocks = build_skills_section(
                skill_names=skill_names,
                sync_pending=False,
                can_edit_spec=True,
            )
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l2_view(
                    agent_name=agent_name_for_remove,
                    active_section=active_section,
                    team_id=team_id,
                    channel_id=channel_id,
                    is_admin=is_admin,
                    can_edit_spec=True,
                    section_blocks=section_blocks,
                ),
            )

        # -----------------------------------------------------------------------
        # Remove MCP — field-conditional: MCP servers are part of the
        # agent spec, so this refuses a non-admin only when the target agent
        # is currently reachable. Re-resolves via the shared gate post-ack.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__remove_mcp":
            is_admin = await resolve_is_admin(client, user_id=user_id)

            mcp_to_remove: str = action.get("selected_option", {}).get("value") or ""
            if not mcp_to_remove or mcp_to_remove == "__none__":
                return

            agent_name_for_remove = selected_agent_name or ""
            if not agent_name_for_remove:
                return

            if await gate.refuse_if_reachable_and_not_admin(
                runtime,
                client,
                tenant_id=tenant_id,
                agent_name=agent_name_for_remove,
                channel_id=channel_id,
                user_id=user_id,
            ):
                return

            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_remove
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_remove,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            ma_agent_full = await runtime.anthropic.beta.agents.retrieve(ma_agent.id)
            current_mcps = [
                {"name": m.name, "url": m.url}
                for m in (ma_agent_full.mcp_servers or [])
                if m.name != mcp_to_remove
            ]
            spec = AgentSpec.model_validate(
                {
                    "name": agent_name_for_remove,
                    "model": ma_agent_full.model.id,
                    "mcp_servers": current_mcps,
                }
            )
            await replace_agent_resources_for_panel(runtime, tenant_id=tenant_id, spec=spec)

            async with runtime.sessionmaker() as session:
                updated_data = await load_section_data(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    agent_name=agent_name_for_remove,
                    section="mcps",
                )
            mcp_names: list[str] = list(updated_data) if isinstance(updated_data, list) else []  # type: ignore[arg-type]
            mcps_dicts = [{"name": n, "url": ""} for n in mcp_names]
            # Reaching here means the gate above proceeded: either admin, or
            # the target was unreachable -- exactly can_edit_spec's definition.
            section_blocks = build_mcps_section(
                mcps=mcps_dicts, can_edit_spec=True, is_admin=is_admin
            )
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l2_view(
                    agent_name=agent_name_for_remove,
                    active_section=active_section,
                    team_id=team_id,
                    channel_id=channel_id,
                    is_admin=is_admin,
                    can_edit_spec=True,
                    section_blocks=section_blocks,
                ),
            )

        # -----------------------------------------------------------------------
        # Remove secret — env-variable credentials are a per-agent attachment,
        # so this routes through the shared-state gate rather than the spec
        # gate: an admin may remove a secret from the built-in agent, but a
        # non-admin may not remove one from any agent the workspace currently
        # depends on. The is_admin resolved below is not that check — it feeds
        # only the L1 stale fallback and the L2 re-render's build_l2_view kwargs.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__remove_secret":
            is_admin = await resolve_is_admin(client, user_id=user_id)

            secret_key_to_remove: str = action.get("selected_option", {}).get("value") or ""
            if not secret_key_to_remove or secret_key_to_remove == "__none__":
                return

            agent_name_for_remove = selected_agent_name or ""
            if not agent_name_for_remove:
                return

            if await gate.refuse_if_shared_and_not_admin(
                runtime,
                client,
                tenant_id=tenant_id,
                agent_name=agent_name_for_remove,
                channel_id=channel_id,
                user_id=user_id,
            ):
                return

            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_remove
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_remove,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            from daimon.core.stores.agent_files import delete_agent_file

            agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
            async with runtime.sessionmaker() as session, session.begin():
                await delete_agent_file(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    key=secret_key_to_remove,
                )

            can_edit_spec = await _compute_can_edit_spec(
                runtime, is_admin=is_admin, tenant_id=tenant_id, agent_name=agent_name_for_remove
            )

            async with runtime.sessionmaker() as session:
                updated_data = await load_section_data(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    agent_name=agent_name_for_remove,
                    section="secrets",
                )
            secret_names: list[str] = list(updated_data) if isinstance(updated_data, list) else []  # type: ignore[arg-type]
            section_blocks = build_secrets_section(
                agent_name=agent_name_for_remove,
                secret_names=secret_names,
            )
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_l2_view(
                    agent_name=agent_name_for_remove,
                    active_section=active_section,
                    team_id=team_id,
                    channel_id=channel_id,
                    is_admin=is_admin,
                    can_edit_spec=can_edit_spec,
                    section_blocks=section_blocks,
                ),
            )

        # -----------------------------------------------------------------------
        # Connect via MCP (ephemeral spill-out) — MUTATION: re-resolve is_admin
        # Modal stays OPEN — no views.update/close after the ephemeral.
        # JWT: never logged, jti only (T-83-11).
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__connect_mcp":
            is_admin = await resolve_is_admin(client, user_id=user_id)
            if not is_admin:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=":x: You no longer have permission to change agent setup.",
                )
                return

            agent_name_for_mcp = selected_agent_name or ""
            if not agent_name_for_mcp:
                return

            public_url = (
                str(runtime.settings.mcp.public_url)
                if runtime.settings.mcp.public_url is not None
                else None
            )
            jwt_secret = runtime.settings.mcp.jwt_secret

            if public_url is None or jwt_secret is None:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=(
                        ":x: MCP URL / JWT secret not configured. "
                        "Ask the operator to set DAIMON_MCP__PUBLIC_URL and DAIMON_MCP__JWT_SECRET."
                    ),
                )
                return

            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_mcp
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_mcp,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
            actor_account_id = await _resolve_actor_account_id(
                runtime, tenant_id=tenant_id, user_id=user_id
            )

            async with runtime.sessionmaker() as session, session.begin():
                token = await mint_agent_mcp_token(
                    session,
                    account_id=actor_account_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    label=agent_name_for_mcp,
                    secret=jwt_secret.get_secret_value().encode(),
                    now=dt.datetime.now(dt.UTC),
                )

            # Decode jti without verifying signature (just minted).
            claims: dict[str, object] = pyjwt.decode(
                token,
                options={"verify_signature": False},
            )  # pyright: ignore[reportAssignmentType]
            jti_val = claims.get("jti")
            log.info(
                "slack.agent_setup.connect_mcp.minted",
                agent_name=agent_name_for_mcp,
                jti=str(jti_val),
                # Token value never logged (T-83-11).
            )

            ephemeral_text = _render_mcp_config_ephemeral(
                agent_name=agent_name_for_mcp,
                public_url=public_url,
                jwt=token,
            )
            # chat.postEphemeral — modal stays OPEN.
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id or user_id,
                user=user_id,
                text=ephemeral_text,
            )

        # -----------------------------------------------------------------------
        # New Agent — push L3 new-agent form. Open to every member:
        # creating an unscoped agent is not tenant-wide blast radius.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__new":
            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_l3_new_agent_form(
                    team_id=team_id,
                    channel_id=channel_id,
                    parent_section=None,
                ),
            )

        # -----------------------------------------------------------------------
        # Fork Agent — push L3 fork-agent form. Open to every member:
        # forking is a new, unscoped agent, same as New.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__fork":
            fork_source = selected_agent_name or ""
            if not fork_source:
                return

            is_admin = await resolve_is_admin(client, user_id=user_id)

            # Stale check (T-83-21): verify source agent still exists.
            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=fork_source
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=fork_source,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_l3_fork_agent_form(
                    source_name=fork_source,
                    team_id=team_id,
                    channel_id=channel_id,
                    parent_section=None,
                ),
            )

        # -----------------------------------------------------------------------
        # Edit Agent Form — push L3 edit-agent form. Field-conditional:
        # touches system/model, part of the agent spec, so this refuses a
        # non-admin only when the target agent is currently reachable.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__edit_agent_form":
            agent_name_for_edit = selected_agent_name or ""
            if not agent_name_for_edit:
                return

            is_admin = await resolve_is_admin(client, user_id=user_id)

            if await gate.refuse_if_reachable_and_not_admin(
                runtime,
                client,
                tenant_id=tenant_id,
                agent_name=agent_name_for_edit,
                channel_id=channel_id,
                user_id=user_id,
            ):
                return

            # Stale check (T-83-21).
            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_edit
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_edit,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            async with runtime.sessionmaker() as session:
                agent_data = await load_section_data(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    agent_name=agent_name_for_edit,
                    section="agent",
                )

            agent_info: dict[str, Any] = dict(agent_data) if isinstance(agent_data, dict) else {}  # type: ignore[arg-type]
            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_l3_edit_agent_form(
                    agent_name=agent_name_for_edit,
                    model_id=str(agent_info.get("model_id") or ""),
                    system_prompt=str(agent_info.get("system_prompt") or ""),
                    team_id=team_id,
                    channel_id=channel_id,
                    parent_section="agent",
                ),
            )

        # -----------------------------------------------------------------------
        # Edit Repo Form — push L3 edit-repo form, gated on shared state at the
        # push. Pushing a form is not itself a write; the reason the gate sits
        # here is that this form prompts for a GitHub personal access token, and
        # no credential should transit for a write that is going to be refused.
        # The submission is gated again, which is the actual write boundary.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__edit_repo_form":
            agent_name_for_repo = selected_agent_name or ""
            if not agent_name_for_repo:
                return

            if await gate.refuse_if_shared_and_not_admin(
                runtime,
                client,
                tenant_id=tenant_id,
                agent_name=agent_name_for_repo,
                channel_id=channel_id,
                user_id=user_id,
            ):
                return

            is_admin = await resolve_is_admin(client, user_id=user_id)

            # Stale check (T-83-21).
            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_repo
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_repo,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_l3_edit_repo_form(
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_repo,
                    parent_section="repo_auth",
                ),
            )

        # -----------------------------------------------------------------------
        # Add Skill — push L3 add-skill form. Field-conditional: skills
        # are part of the agent spec, so this refuses a non-admin only when
        # the target agent is currently reachable.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__add_skill":
            agent_name_for_skill = selected_agent_name or ""
            if not agent_name_for_skill:
                return

            is_admin = await resolve_is_admin(client, user_id=user_id)

            if await gate.refuse_if_reachable_and_not_admin(
                runtime,
                client,
                tenant_id=tenant_id,
                agent_name=agent_name_for_skill,
                channel_id=channel_id,
                user_id=user_id,
            ):
                return

            # Stale check (T-83-21).
            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_skill
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_skill,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_l3_add_skill_form(
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_skill,
                    parent_section="skills",
                ),
            )

        # -----------------------------------------------------------------------
        # Add MCP Server — push L3 add-mcp form. Field-conditional:
        # MCP servers are part of the agent spec, so this refuses a
        # non-admin only when the target agent is currently reachable.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__add_mcp":
            agent_name_for_mcp = selected_agent_name or ""
            if not agent_name_for_mcp:
                return

            is_admin = await resolve_is_admin(client, user_id=user_id)

            if await gate.refuse_if_reachable_and_not_admin(
                runtime,
                client,
                tenant_id=tenant_id,
                agent_name=agent_name_for_mcp,
                channel_id=channel_id,
                user_id=user_id,
            ):
                return

            # Stale check (T-83-21).
            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_mcp
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_mcp,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_l3_add_mcp_form(
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_mcp,
                    parent_section="mcps",
                ),
            )

        # -----------------------------------------------------------------------
        # Paste Secrets — push L3 paste-secrets form, deliberately ungated at
        # the push, unlike the adjacent edit-repo form: this form prompts for
        # nothing at push time, so no credential can transit for a write that
        # will be refused. Every member gets the form; the write it submits is
        # refused at submission instead, which is the actual write boundary.
        # -----------------------------------------------------------------------
        elif action_id == "agent_setup__paste_secrets":
            agent_name_for_secrets = selected_agent_name or ""
            if not agent_name_for_secrets:
                return

            is_admin = await resolve_is_admin(client, user_id=user_id)

            # Stale check (T-83-21).
            ma_agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=agent_name_for_secrets
            )
            if ma_agent is None:
                await _render_stale_l1(
                    client,
                    view_id=view_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_secrets,
                    is_admin=is_admin,
                    runtime=runtime,
                    tenant_id=tenant_id,
                )
                return

            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_l3_paste_secrets_form(
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_name=agent_name_for_secrets,
                    parent_section="secrets",
                ),
            )

        else:
            log.debug(
                "slack.agent_setup_action.unknown_action_id",
                action_id=action_id,
                team_id=team_id,
            )

    except (DaimonError, anthropic.APIError, SlackApiError, InvalidToken, SQLAlchemyError) as exc:
        log.error(
            "slack.agent_setup_action_failed",
            team_id=team_id,
            action_id=action_id,
            exc_info=exc,
        )
        capture_exception_with_scope(exc)

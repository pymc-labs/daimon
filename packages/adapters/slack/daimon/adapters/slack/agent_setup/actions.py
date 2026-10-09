"""Slack /agent-setup slash handler + block_action interactive handlers.

Shell module: all I/O lives here. The pure builders (panel_views.py), the
metadata reducers (state.py) and the read path (read.py) are called but never
catch exceptions — failures propagate to the listener-boundary catch in this
module.

Handler contract:
  handle_agent_setup_command(runtime, payload)
    Slash-command entry: open the loading modal on the fresh trigger_id →
    background-fetch roster, role and attributions → views.update with the
    Agents view. On failure: update to the error view, never an infinite
    spinner.

  handle_agent_setup_action(runtime, payload)
    Block-action dispatch for action_id matching ``agent_setup__*``. Ack-first
    discipline (app.py acks before spawning this handler). All I/O is wrapped
    in the boundary catch, which renders into the open view rather than
    failing silently.

The panel's three main screens — Agents, one agent's Details, and
Who answers where — are dispatched by ``PANEL_ACTION_IDS`` and read their
state from the typed ``PanelMetadata`` the views carry, never from the click.
Navigation pushes; paging and the Details expansions update the view they were
clicked on, so the stack never grows past the two depths the design uses.

The avatar controls are admin-only writes from Details. Other clicks leave
the read path for these existing workflows:
  - New agent pushes the creation form, whose submission is handled in
    submit.py. Creating an unscoped agent has no tenant-wide blast radius, so
    it is open to every member.
  - Use from your coding tools mints a scoped bearer token behind a live admin
    check resolved post-ack, server-side (hiding ≠ gating). Token values
    are never logged, not even in part.
  - Channel admins, on Who answers where, pushes the form naming this
    channel's admins, submitted in channel_admins.py. Workspace admins only,
    re-checked live on the click and on the submission.
  - Channel skills, on Who answers where, pushes the form adding or removing
    this channel's extra skills, submitted in channel_skills.py. Workspace
    admins only, re-checked live on the click and on the submission.
  - This channel's environment, on Who answers where, saves the pick through
    channel_environment.py. Workspace admins and this channel's admins,
    re-checked live on the pick.
  - The setup-conversation button opens a thread with Daimon for other agent
    and routing changes, where the chat tool owns the authorization.

A click whose view carries no panel metadata belongs to a surface this
deployment no longer serves; it is logged at debug and dropped rather than
acted on.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import aiohttp
import anthropic
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_policy import (
    refuse_unless_allowed_for_agent_name,
    refuse_unless_pin_allows,
)
from daimon.adapters.slack.agent_setup import github_add_repos as github_add_repos_view
from daimon.adapters.slack.agent_setup import (
    github_manage,
    github_waiting,
    panel_views,
)
from daimon.adapters.slack.agent_setup import github_repos as github_repos_view
from daimon.adapters.slack.agent_setup.channel_environment import (
    ENVIRONMENT_NEED_ADMIN_MESSAGE,
    load_environment_picker,
    load_picker_subject,
    may_pick_environment,
    save_environment_choice,
)
from daimon.adapters.slack.agent_setup.coding_tools import (
    handle_coding_tools_click,
    handle_revoke_token_click,
)
from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.agent_setup.github_new_repo import (
    send_pending_notice as send_pending_new_repo_notice,
)
from daimon.adapters.slack.agent_setup.github_removal import (
    send_pending_notice as send_pending_removal_notice,
)
from daimon.adapters.slack.agent_setup.github_repos_actions import handle as handle_github_repos
from daimon.adapters.slack.agent_setup.panel_views import (
    build_agents_view,
    build_details_view,
    build_error_view,
    build_new_agent_form,
    build_routing_view,
)
from daimon.adapters.slack.agent_setup.read import (
    coding_tools_available,
    load_panel_answering_map,
    load_panel_details,
    load_panel_roster,
    resolve_attributions,
)
from daimon.adapters.slack.agent_setup.rules import RULE_NEED_ADMIN_MESSAGE, change_rule
from daimon.adapters.slack.agent_setup.state import (
    PANEL_PAGE_SIZE,
    PanelExpansion,
    PanelMetadata,
    decode_panel_metadata,
    encode_panel_metadata,
)
from daimon.adapters.slack.channel_admin_groups import list_user_groups
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.errors import render_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.modal_limits import fit_title
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.setup_conversations import (
    create_setup_conversation,
    setup_link,
    setup_reply_button,
)
from daimon.core.agent_identity import identity_enabled_for, resolve_agent_identity
from daimon.core.answering_map import AnsweringMap, routed_agent_names
from daimon.core.channel_admins import GroupLookupFailed
from daimon.core.channel_rules import as_readers, as_writers, channel_rule_status
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.errors import DaimonError
from daimon.core.github_panel import connect_link, pending_connect_link, safe_github_error
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.models_catalog import list_model_choices
from daimon.core.observability import capture_exception_with_scope
from daimon.core.panel_audit import record_panel_write
from daimon.core.panel_operator_tokens import list_panel_operator_tokens
from daimon.core.roster import Roster, paginate
from daimon.core.setup_conversations import setup_thread_name
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.channel_admins import (
    get_channel_admins,
    list_administered_channel_ids,
    list_channel_admins,
)
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access_requests import list_asker_requests
from daimon.core.stores.github_connected_repos import summary as connected_summary
from daimon.core.stores.github_links import account_link_status
from daimon.core.stores.github_personal_links import mint_link
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

OPERATOR_TOKENS_NEED_ADMIN_MESSAGE = "Only a workspace admin can mint or revoke operator tokens."
CHANNEL_ADMINS_NEED_ADMIN_MESSAGE = (
    "Only a workspace admin can name a channel's admins. Nothing changed."
)
CHANNEL_SKILLS_NEED_ADMIN_MESSAGE = (
    "Only a workspace admin can change a channel's skills. Nothing changed."
)


async def github_can_choose_agent(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, user_id: str, is_admin: bool
) -> bool:
    if is_admin:
        return True
    async with runtime.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        account = await get_account(session, principal.account_id)
        if account is None or account.is_external:
            return False
        grants = await list_administered_channel_ids(
            session,
            tenant_id=tenant_id,
            platform="slack",
            platform_user_id=user_id,
            role_ids=account.platform_role_ids,
        )
    return bool(grants)


# ---------------------------------------------------------------------------
# Request ID for error views
# ---------------------------------------------------------------------------


def _new_request_id() -> str:
    """Generate a short opaque request ID for error cross-referencing."""
    return str(uuid.uuid4())[:8]


async def github_pending_url(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, user_id: str
) -> str | None:
    async with runtime.sessionmaker.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        account = await get_account(session, principal.account_id)
        if account is None or account.is_external:
            return None
        return await pending_connect_link(
            session,
            settings=runtime.settings,
            tenant_id=tenant_id,
            requester_account_id=account.id,
        )


async def github_personal_login(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, user_id: str
) -> str | None:
    async with runtime.sessionmaker.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        return await account_link_status(session, account_id=principal.account_id)


async def github_home_view(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    meta: PanelMetadata,
    user_id: str,
    is_admin: bool,
) -> dict[str, Any]:
    """Rebuild the complete GitHub home after a modal action."""
    async with runtime.sessionmaker() as session:
        home = await connected_summary(session, tenant_id=tenant_id) if is_admin else None
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        own = await list_asker_requests(
            session, tenant_id=tenant_id, account_id=principal.account_id
        )
        linked = await account_link_status(session, account_id=principal.account_id)
    return panel_views.build_github_home_view(
        dataclasses.replace(meta, github_step="pick"),
        connected_count=home.count if home else 0,
        is_admin=is_admin,
        owners=home.owners if home else (),
        agent_count=home.agent_count if home else 0,
        pending_url=(
            await github_pending_url(runtime, tenant_id=tenant_id, user_id=user_id)
            if is_admin
            else None
        ),
        linked_login=linked,
        own_waiting_count=len(own),
        can_choose_agent=await github_can_choose_agent(
            runtime, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
        ),
    )


# ---------------------------------------------------------------------------
# Slash-command entry
# ---------------------------------------------------------------------------


async def handle_agent_setup_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Slash command handler for /agent-setup (loading-modal pattern).

    Immediately opens a "Agents" modal with the fresh trigger_id (beats the ~3s
    expiry), then background-loads the roster, the caller's admin status and the
    creator attributions, and updates the modal in place with the Agents view.

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
    # Slack omits thread_ts from slash payloads outside a thread; when it is
    # there the roster answers for the thread's own responder, not the parent.
    thread_id: str | None = payload.get("thread_ts") or None

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.agent_setup_command.no_token", team_id=team_id)
        return

    view_id: str = ""
    meta = PanelMetadata(
        team_id=team_id,
        channel_id=channel_id,
        thread_id=thread_id,
        view="agents",
        channel_name=str(payload.get("channel_name") or "")[:80] or None,
    )
    try:
        # Open loading modal immediately — must beat the ~3s trigger_id TTL.
        resp = await client.views_open(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=_build_panel_loading_view(meta),
        )
        view_id = str(resp["view"]["id"])  # pyright: ignore[reportUnknownArgumentType, reportOptionalSubscript]

        # Slow path (off the 3s window): tenant, role, roster, attributions.
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        is_admin = await resolve_is_admin(client, user_id=user_id)

        async with runtime.sessionmaker() as session:
            roster = await load_panel_roster(
                session,
                runtime.anthropic,
                tenant_id=tenant_id,
                channel_id=channel_id or None,
                thread_id=thread_id,
                default=runtime.deployment_default,
                is_admin=is_admin,
            )
            answering_map = await load_panel_answering_map(
                session,
                runtime.anthropic,
                tenant_id=tenant_id,
                default=runtime.deployment_default,
                channel_id=channel_id or None,
                is_admin=is_admin,
            )
            attributions = await resolve_attributions(
                session,
                tenant_id=tenant_id,
                account_ids=_roster_account_ids(roster),
            )

        if payload.get("command") == "/github":
            async with runtime.sessionmaker() as session:
                home = await connected_summary(session, tenant_id=tenant_id) if is_admin else None
                principal = await get_or_create_platform_principal(
                    session, tenant_id=tenant_id, platform="slack", external_id=user_id
                )
                own_waiting = await list_asker_requests(
                    session, tenant_id=tenant_id, account_id=principal.account_id
                )
            pending_url = (
                await github_pending_url(runtime, tenant_id=tenant_id, user_id=user_id)
                if is_admin
                else None
            )
            rendered = panel_views.build_github_home_view(
                meta,
                connected_count=home.count if home else 0,
                is_admin=is_admin,
                owners=home.owners if home else (),
                agent_count=home.agent_count if home else 0,
                pending_url=pending_url,
                linked_login=await github_personal_login(
                    runtime, tenant_id=tenant_id, user_id=user_id
                ),
                own_waiting_count=len(own_waiting),
                can_choose_agent=await github_can_choose_agent(
                    runtime, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
                ),
            )
        else:
            rendered = build_agents_view(
                roster,
                page=paginate(roster.rows, page=0, page_size=PANEL_PAGE_SIZE),
                meta=meta,
                is_admin=is_admin,
                attributions=attributions,
                channel_id=channel_id,
                routed_agent_names=routed_agent_names(answering_map),
            )
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,
            view=rendered,
        )
        if is_admin:
            await send_pending_new_repo_notice(
                runtime, client, team_id=team_id, channel_id=channel_id, user_id=user_id
            )
            await send_pending_removal_notice(
                runtime,
                client,
                team_id=team_id,
                channel_id=channel_id,
                user_id=user_id,
                response_url=str(payload.get("response_url") or "") or None,
            )

    except (
        DaimonError,
        anthropic.APIError,
        SlackApiError,
        InvalidToken,
        SQLAlchemyError,
        aiohttp.ClientError,
        TimeoutError,
    ) as exc:
        log.error("slack.agent_setup_command_failed", team_id=team_id, exc_info=exc)
        capture_exception_with_scope(exc)
        # No infinite spinner — update to error view on failure.
        if view_id:
            request_id = _new_request_id()
            # Swallow secondary failure — best-effort error render.
            with contextlib.suppress(Exception):
                await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                    view_id=view_id,
                    view=build_error_view(request_id=request_id),
                )


# ---------------------------------------------------------------------------
# Panel views (Agents / Details / Who answers where)
# ---------------------------------------------------------------------------

_RULE_ACTIONS = frozenset(
    {
        panel_views.ACTION_RULE_READERS,
        panel_views.ACTION_RULE_WRITERS,
        panel_views.ACTION_RULE_COPY,
        panel_views.ACTION_RULE_RELEASE,
    }
)

#: Every action id the three panel views emit. Kept as a set so the dispatcher
#: routes the in-view ones in one branch and leaves the legacy editor ids to
#: the chain below it. Revoke is in here too, but reaches its handler earlier:
#: it is clicked on an ephemeral, which carries no view and so no metadata.
PANEL_ACTION_IDS: frozenset[str] = frozenset(
    {
        panel_views.ACTION_DETAILS,
        panel_views.ACTION_AVATAR_CHANGE,
        panel_views.ACTION_AVATAR_DETAILS,
        panel_views.ACTION_AVATAR_RESET,
        panel_views.ACTION_ROUTING,
        panel_views.ACTION_PAGE_NEXT,
        panel_views.ACTION_PAGE_PREV,
        panel_views.ACTION_EXPAND_KEYS,
        panel_views.ACTION_EXPAND_SKILLS,
        panel_views.ACTION_EXPAND_CONNECTIONS,
        panel_views.ACTION_NEW,
        panel_views.ACTION_GITHUB_CONNECT,
        panel_views.ACTION_GITHUB_START,
        panel_views.ACTION_GITHUB_CHOOSE_AGENT,
        panel_views.ACTION_GITHUB_BACK,
        panel_views.ACTION_GITHUB_MANAGE,
        panel_views.ACTION_GITHUB_PERSONAL_LINK,
        panel_views.ACTION_GITHUB_UNLINK,
        panel_views.ACTION_GITHUB_UNLINK_CONFIRM,
        panel_views.ACTION_GITHUB_UNLINK_BACK,
        *github_manage.ACTIONS,
        *github_waiting.ACTIONS,
        github_repos_view.ACTION_OPEN,
        github_repos_view.ACTION_SELECT,
        github_repos_view.ACTION_STAGE,
        github_repos_view.ACTION_REMOVE,
        github_repos_view.ACTION_ACTIVATE,
        github_repos_view.ACTION_DEACTIVATE,
        github_repos_view.ACTION_PREVIOUS,
        github_repos_view.ACTION_NEXT,
        github_repos_view.ACTION_CONFIRM_REMOVE,
        github_repos_view.ACTION_CONFIRM_TURN_OFF,
        github_repos_view.ACTION_CANCEL_CONFIRM,
        github_repos_view.ACTION_ADD_OPEN,
        github_repos_view.ACTION_BACK,
        github_repos_view.ACTION_SETTINGS,
        github_repos_view.ACTION_SETTINGS_CHOICE,
        *github_add_repos_view.ACTIONS,
        panel_views.ACTION_CODING_TOOLS,
        panel_views.ACTION_ADD_SKILL,
        panel_views.ACTION_REVOKE_TOKEN,
        panel_views.ACTION_CHANNEL_ADMINS,
        panel_views.ACTION_CHANNEL_SKILLS,
        panel_views.ACTION_OPERATOR_MINT,
        panel_views.ACTION_OPERATOR_REVOKE,
        panel_views.ACTION_ENVIRONMENT,
        *_RULE_ACTIONS,
    }
)


def _build_panel_loading_view(meta: PanelMetadata) -> dict[str, Any]:
    """The modal opened on the trigger_id, before any read has happened.

    Carries the root view's own title and metadata so the swap to the loaded
    Agents view never looks like a different modal.
    """
    return {
        "type": "modal",
        "callback_id": "agent_setup",
        "private_metadata": encode_panel_metadata(meta),
        "title": {"type": "plain_text", "text": fit_title("Agents")},
        "close": {"type": "plain_text", "text": "Done"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Loading…"}}],
    }


def _roster_account_ids(roster: Roster) -> list[uuid.UUID]:
    """The creator accounts a roster render can attribute rows to."""
    return [row.created_by_account_id for row in roster.rows if row.created_by_account_id]


def _answering_map_account_ids(answering_map: AnsweringMap) -> list[uuid.UUID]:
    """Every account the routing view may name: who set a tier, who opened a setup."""
    ids = [
        row.set_by_account_id for row in answering_map.channel_overrides if row.set_by_account_id
    ]
    if answering_map.tenant_default is not None and answering_map.tenant_default.set_by_account_id:
        ids.append(answering_map.tenant_default.set_by_account_id)
    ids.extend(
        ref.creator_account_id for ref in answering_map.setup_threads if ref.creator_account_id
    )
    return ids


def _with_stale_notice(view: dict[str, Any], *, agent_name: str) -> dict[str, Any]:
    """Prepend the stale-agent warning to an already-built Agents view.

    The agent a click named is gone; the list beside the notice is the fresh
    one, so the reader sees what happened and what is actually there.
    """
    blocks: list[dict[str, Any]] = list(view.get("blocks") or [])
    blocks.insert(
        0,
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f":warning: `{escape_mrkdwn(agent_name)}` is no longer available "
                        "— it may have been deleted. Showing the current list."
                    ),
                }
            ],
        },
    )
    return {**view, "blocks": blocks}


async def load_agents_view(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    meta: PanelMetadata,
    is_admin: bool,
) -> dict[str, Any]:
    """Build the Agents view for `meta`'s page.

    Shared with the new-agent submission, which re-renders this same view on
    the root once the created agent exists.
    """
    async with runtime.sessionmaker() as session:
        roster = await load_panel_roster(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            channel_id=meta.channel_id or None,
            thread_id=None,
            default=runtime.deployment_default,
            is_admin=is_admin,
        )
        answering_map = await load_panel_answering_map(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            default=runtime.deployment_default,
            channel_id=meta.channel_id or None,
            is_admin=is_admin,
        )
        attributions = await resolve_attributions(
            session, tenant_id=tenant_id, account_ids=_roster_account_ids(roster)
        )
    page = paginate(roster.rows, page=meta.page, page_size=PANEL_PAGE_SIZE)
    return build_agents_view(
        roster,
        page=page,
        meta=meta.with_page(page.page),
        is_admin=is_admin,
        attributions=attributions,
        channel_id=meta.channel_id,
        routed_agent_names=routed_agent_names(answering_map),
    )


async def load_routing_view(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    meta: PanelMetadata,
    is_admin: bool,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Build the Who-answers-where view for `meta`'s page; admins also get the channel
    admins and this channel's permissions.

    `user_id` lets an admin of this channel see its environment select;
    workspace admins get it without one.
    """
    async with runtime.sessionmaker() as session:
        answering_map = await load_panel_answering_map(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            default=runtime.deployment_default,
            channel_id=meta.channel_id or None,
            is_admin=is_admin,
        )
        channel_admins = (
            await list_channel_admins(session, tenant_id=tenant_id, platform="slack")
            if is_admin
            else None
        )
        operator_tokens = (
            await list_panel_operator_tokens(session, tenant_id=tenant_id, now=datetime.now(UTC))
            if is_admin and runtime.settings.mcp.jwt_secret is not None
            else None
        )
        rule_status = (
            channel_rule_status(
                await load_access_policy(session, tenant_id=tenant_id), meta.channel_id
            )
            if is_admin and meta.channel_id
            else None
        )
        channel_skills = (
            await list_channel_skills(
                session, tenant_id=tenant_id, platform="slack", channel_id=meta.channel_id
            )
            if is_admin and meta.channel_id
            else None
        )
        attributions = await resolve_attributions(
            session, tenant_id=tenant_id, account_ids=_answering_map_account_ids(answering_map)
        )
        roster = await load_panel_roster(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            channel_id=meta.channel_id or None,
            thread_id=None,
            default=runtime.deployment_default,
            is_admin=is_admin,
        )
    setup_links = [
        f"<{setup_link(meta.team_id, ref.parent_channel_id, ref.thread_id)}|"
        f"{setup_thread_name(escape_mrkdwn(ref.target_name) if ref.target_name else None)}>"
        for ref in answering_map.setup_threads
    ]
    # The routing request names one agent nobody can reach yet; the built-in
    # agent is never it, since the deployment default already answers for it.
    unrouted_agent_name = next(
        (row.name for row in roster.rows if row.answering_tier is None and not row.is_built_in),
        None,
    )
    environment_picker = await load_environment_picker(
        runtime,
        client,
        tenant_id=tenant_id,
        answering_map=answering_map,
        channel_id=meta.channel_id,
        user_id=user_id,
        is_admin=is_admin,
    )
    page = paginate(answering_map.channel_overrides, page=meta.page, page_size=PANEL_PAGE_SIZE)
    return build_routing_view(
        answering_map,
        page=page,
        meta=meta.with_page(page.page),
        is_admin=is_admin,
        attributions=attributions,
        setup_links=setup_links,
        channel_id=meta.channel_id,
        unrouted_agent_name=unrouted_agent_name,
        channel_admins=channel_admins,
        environment_picker=environment_picker,
        rule_status=rule_status,
        operator_tokens=operator_tokens,
        channel_skills=channel_skills,
    )


async def load_details_view(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    meta: PanelMetadata,
    agent_name: str,
    is_admin: bool,
) -> dict[str, Any] | None:
    """Build the Details view for `agent_name`, or None when it is gone."""
    async with runtime.sessionmaker() as session:
        roster = await load_panel_roster(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            channel_id=meta.channel_id or None,
            thread_id=None,
            default=runtime.deployment_default,
            is_admin=is_admin,
        )
        details = await load_panel_details(
            session,
            runtime.anthropic,
            runtime,
            tenant_id=tenant_id,
            roster=roster,
            agent_name=agent_name,
            channel_id=meta.channel_id,
            thread_id=None,
            is_admin=is_admin,
        )
        if details is None:
            return None
        attributions = await resolve_attributions(
            session,
            tenant_id=tenant_id,
            account_ids=[details.created_by_account_id] if details.created_by_account_id else [],
        )
        avatar_url: str | None = None
        if (
            identity_enabled_for(runtime.settings, "slack", meta.team_id)
            and not details.daimon_managed
            and details.name != runtime.deployment_default.agent_name
        ):
            identity = await resolve_agent_identity(
                session,
                tenant_id=tenant_id,
                agent_name=details.name,
                is_builtin=False,
                public_base_url=runtime.settings.mcp.app_root_url,
                enabled=True,
                background_sessionmaker=runtime.sessionmaker,
            )
            avatar_url = identity.avatar_url
    view = build_details_view(
        details,
        meta=meta.with_view("details", agent_name=agent_name),
        is_admin=is_admin,
        coding_tools_available=coding_tools_available(runtime),
        channel_id=meta.channel_id,
        attribution=attributions.get(details.created_by_account_id)
        if details.created_by_account_id
        else None,
        avatar_url=avatar_url,
        avatar_editable=(
            identity_enabled_for(runtime.settings, "slack", meta.team_id)
            and not details.daimon_managed
            and details.name != runtime.deployment_default.agent_name
        ),
    )
    return view


async def _update_paged_view(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    payload: dict[str, Any],
    *,
    tenant_id: uuid.UUID,
    meta: PanelMetadata,
    is_admin: bool,
    user_id: str,
    delta: int,
) -> None:
    """Re-render the current view one page over, in place.

    Never pushes: a pager that pushed would burn one of the three view slots
    per click. The view hash is sent so two clicks racing on the same view
    lose the second one rather than overwriting each other; Slack answers that
    with `hash_conflict`, which means the view has already moved on and the
    click has nothing left to do.
    """
    view_info: dict[str, Any] = payload.get("view") or {}
    target = meta.with_page(max(meta.page + delta, 0))
    view = (
        await load_routing_view(
            runtime, client, tenant_id=tenant_id, meta=target, is_admin=is_admin, user_id=user_id
        )
        if meta.view == "routing"
        else await load_agents_view(runtime, tenant_id=tenant_id, meta=target, is_admin=is_admin)
    )
    try:
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=str(view_info.get("id") or ""),
            hash=str(view_info.get("hash") or ""),
            view=view,
        )
    except SlackApiError as exc:
        if _is_hash_conflict(exc):
            log.info(
                "slack.agent_setup.page.hash_conflict",
                view=meta.view,
                page=target.page,
            )
            return
        raise


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """A uuid from a button value, or None when the value is not one."""
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _is_hash_conflict(exc: SlackApiError) -> bool:
    response: Any = exc.response  # pyright: ignore[reportUnknownMemberType]  # SlackApiError.response is untyped
    return str(response.get("error") or "") == "hash_conflict"


async def _dispatch_panel_action(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    payload: dict[str, Any],
    *,
    action: dict[str, Any],
    action_id: str,
    meta: PanelMetadata,
    tenant_id: uuid.UUID,
    team_id: str,
    user_id: str,
) -> None:
    """Handle one click from the three read-only panel views.

    Navigation pushes (Details, Who answers where, New agent); paging and the
    Details expansions update the view they were clicked on. Every branch
    re-resolves the caller's role rather than trusting the rendered view.
    """
    view_info: dict[str, Any] = payload.get("view") or {}
    view_id: str = str(view_info.get("id") or "")
    view_hash: str = str(view_info.get("hash") or "")
    trigger_id: str = str(payload.get("trigger_id") or "")
    is_admin = await resolve_is_admin(client, user_id=user_id)

    if action_id == panel_views.ACTION_AVATAR_CHANGE:
        if (
            not identity_enabled_for(runtime.settings, "slack", meta.team_id)
            or not meta.agent_name
            or not is_admin
        ):
            await record_panel_write(
                runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                op="agent_avatar_change",
                outcome="denied",
                reason="needs_admin_or_agent_gone",
                agent_name=meta.agent_name,
            )
            if not identity_enabled_for(runtime.settings, "slack", meta.team_id) and view_id:
                await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                    view_id=view_id,
                    view=panel_views.build_avatar_status_view(
                        meta=meta, message="Agent pictures are turned off."
                    ),
                )
            return
        form = panel_views.build_avatar_upload_form(
            meta=meta.with_view(
                "avatar_upload",
                agent_name=meta.agent_name,
                root_view_id=meta.root_view_id or view_id,
            )
        )
        if meta.view == "avatar_upload":
            await client.views_update(view_id=view_id, view=form)  # pyright: ignore[reportUnknownMemberType]
        else:
            await client.views_push(trigger_id=trigger_id, view=form)  # pyright: ignore[reportUnknownMemberType]
        return

    if action_id == panel_views.ACTION_AVATAR_DETAILS:
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=panel_views.build_avatar_details_view(meta=meta),
        )
        return

    if action_id == panel_views.ACTION_AVATAR_RESET:
        from daimon.adapters.slack.agent_setup.avatar import reset_agent_avatar

        await reset_agent_avatar(
            runtime, client, meta=meta, team_id=team_id, user_id=user_id, view_id=view_id
        )
        return

    if await github_manage.handle(
        runtime,
        client,
        payload,
        action=action,
        meta=meta,
        team_id=team_id,
        user_id=user_id,
    ):
        return

    if await github_waiting.handle(
        runtime,
        client,
        payload,
        action=action,
        meta=meta,
        team_id=team_id,
        user_id=user_id,
    ):
        return

    if await handle_github_repos(
        runtime,
        client,
        payload,
        action=action,
        meta=meta,
        team_id=team_id,
        user_id=user_id,
    ):
        return

    if action_id == panel_views.ACTION_GITHUB_PERSONAL_LINK:
        root = runtime.settings.mcp.app_root_url
        if root is None:
            await post_ephemeral(
                client,
                channel_id=meta.channel_id or user_id,
                user_id=user_id,
                text="GitHub didn't answer. Try again in a minute.",
            )
            return
        async with runtime.sessionmaker.begin() as session:
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant_id, platform="slack", external_id=user_id
            )
            url = await mint_link(
                session,
                tenant_id=tenant_id,
                account_id=principal.account_id,
                platform="slack",
                platform_user_id=user_id,
                root_url=str(root),
            )
        card_text = "Link GitHub to your account."
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=meta.channel_id or user_id,
            user=user_id,
            text=card_text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": card_text}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "github_personal__open",
                            "text": {"type": "plain_text", "text": "Link GitHub"},
                            "url": url,
                        }
                    ],
                },
            ],
        )
        return

    if action_id in (
        panel_views.ACTION_GITHUB_UNLINK,
        panel_views.ACTION_GITHUB_UNLINK_CONFIRM,
        panel_views.ACTION_GITHUB_UNLINK_BACK,
    ):
        from daimon.core.stores.github_links import unlink_verified_identity

        if action_id == panel_views.ACTION_GITHUB_UNLINK_CONFIRM:
            if meta.github_step != "personal_unlink":
                return
            async with runtime.sessionmaker.begin() as session:
                principal = await get_or_create_platform_principal(
                    session, tenant_id=tenant_id, platform="slack", external_id=user_id
                )
                await unlink_verified_identity(
                    session,
                    tenant_id=tenant_id,
                    platform="slack",
                    platform_user_id=user_id,
                    account_id=principal.account_id,
                )
        if action_id == panel_views.ACTION_GITHUB_UNLINK:
            next_meta = dataclasses.replace(meta, github_step="personal_unlink")
            view = panel_views.build_github_home_view(
                next_meta, connected_count=0, is_admin=is_admin
            )
        else:
            view = await github_home_view(
                runtime, tenant_id=tenant_id, meta=meta, user_id=user_id, is_admin=is_admin
            )
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id, view=view
        )
        return

    if action_id == panel_views.ACTION_GITHUB_CONNECT:
        if not is_admin:
            await post_ephemeral(
                client,
                channel_id=meta.channel_id or user_id,
                user_id=user_id,
                text="Only a workspace admin can connect GitHub.",
            )
            return
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=await github_home_view(
                runtime, tenant_id=tenant_id, meta=meta, user_id=user_id, is_admin=is_admin
            ),
        )
        return

    if action_id in (
        panel_views.ACTION_GITHUB_CHOOSE_AGENT,
        panel_views.ACTION_GITHUB_BACK,
    ):
        choose_allowed = await github_can_choose_agent(
            runtime, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
        )
        if action_id == panel_views.ACTION_GITHUB_CHOOSE_AGENT and not choose_allowed:
            return
        roster_view = await load_agents_view(
            runtime, tenant_id=tenant_id, meta=meta, is_admin=is_admin
        )
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id, view=roster_view
        )
        return

    if action_id == panel_views.ACTION_GITHUB_START:
        if not is_admin:
            return
        async with runtime.sessionmaker() as session:
            roster = await load_panel_roster(
                session,
                runtime.anthropic,
                tenant_id=tenant_id,
                channel_id=meta.channel_id or None,
                thread_id=None,
                default=runtime.deployment_default,
                is_admin=is_admin,
            )
        agent = roster.answering or (roster.rows[0] if len(roster.rows) == 1 else None)
        if agent is None and roster.rows:
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=await load_agents_view(
                    runtime, tenant_id=tenant_id, meta=meta, is_admin=is_admin
                ),
            )
            return
        team_data = cast("dict[str, Any]", payload.get("team") or {})
        user_data = cast("dict[str, Any]", payload.get("user") or {})
        try:
            async with runtime.sessionmaker.begin() as session:
                principal = await get_or_create_platform_principal(
                    session, tenant_id=tenant_id, platform="slack", external_id=user_id
                )
                await set_role(session, principal.account_id, Role.ADMIN)
                url = await connect_link(
                    session,
                    settings=runtime.settings,
                    tenant_id=tenant_id,
                    platform="slack",
                    platform_user_id=user_id,
                    verified_tenant_admin=True,
                    workspace_label=str(team_data.get("name") or "") or None,
                    requester_label=str(user_data.get("name") or "") or None,
                    start_over=await github_pending_url(
                        runtime, tenant_id=tenant_id, user_id=user_id
                    )
                    is not None,
                    agent_id=(
                        derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id)
                        if agent is not None
                        else None
                    ),
                    agent_name=agent.name if agent is not None else None,
                    origin_parent_channel_id=meta.channel_id or None,
                    origin_thread_id=meta.thread_id,
                    origin_followup_token=str(payload.get("response_url") or "") or None,
                    origin_followup_expires_at=(
                        datetime.now(UTC) + timedelta(minutes=30)
                        if payload.get("response_url")
                        else None
                    ),
                )
        except ValueError as error:
            await post_ephemeral(
                client,
                channel_id=meta.channel_id or user_id,
                user_id=user_id,
                text=safe_github_error(error),
            )
            return
        await send_link(
            client,
            channel_id=meta.channel_id or user_id,
            thread_id=meta.thread_id,
            user_id=user_id,
            url=url,
        )
        return

    if action_id == panel_views.ACTION_DETAILS:
        agent_name = str(action.get("value") or "")
        if not agent_name:
            return
        details_view = await load_details_view(
            runtime, tenant_id=tenant_id, meta=meta, agent_name=agent_name, is_admin=is_admin
        )
        if details_view is None:
            stale_view = await load_agents_view(
                runtime, tenant_id=tenant_id, meta=meta, is_admin=is_admin
            )
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=_with_stale_notice(stale_view, agent_name=agent_name),
            )
            return
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=details_view,
        )
        return

    if action_id == panel_views.ACTION_ROUTING:
        view = await load_routing_view(
            runtime,
            client,
            tenant_id=tenant_id,
            meta=meta.with_view("routing"),
            is_admin=is_admin,
            user_id=user_id,
        )
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=view,
        )
        return

    if action_id in {panel_views.ACTION_PAGE_NEXT, panel_views.ACTION_PAGE_PREV}:
        await _update_paged_view(
            runtime,
            client,
            payload,
            tenant_id=tenant_id,
            meta=meta,
            is_admin=is_admin,
            user_id=user_id,
            delta=1 if action_id == panel_views.ACTION_PAGE_NEXT else -1,
        )
        return

    expansion_actions: dict[str, PanelExpansion] = {
        panel_views.ACTION_EXPAND_KEYS: "keys",
        panel_views.ACTION_EXPAND_SKILLS: "skills",
        panel_views.ACTION_EXPAND_CONNECTIONS: "connections",
    }
    if action_id in expansion_actions:
        agent_name = meta.agent_name or ""
        if not agent_name:
            return
        expansion = expansion_actions[action_id]
        details_view = await load_details_view(
            runtime,
            tenant_id=tenant_id,
            meta=meta.toggled(expansion),
            agent_name=agent_name,
            is_admin=is_admin,
        )
        if details_view is None:
            stale_view = await load_agents_view(
                runtime,
                tenant_id=tenant_id,
                meta=meta.with_view("agents"),
                is_admin=is_admin,
            )
            details_view = _with_stale_notice(stale_view, agent_name=agent_name)
        try:
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                hash=view_hash,
                view=details_view,
            )
        except SlackApiError as exc:
            if _is_hash_conflict(exc):
                log.info(
                    "slack.agent_setup.expansion.hash_conflict",
                    agent_name=agent_name,
                    expansion=expansion,
                )
                return
            raise
        return

    if action_id == panel_views.ACTION_NEW:
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=build_new_agent_form(
                meta=meta.with_view("new_agent", root_view_id=view_id),
                model_choices=list_model_choices(default=DEFAULT_AGENT_MODEL),
            ),
        )
        return

    if action_id == panel_views.ACTION_CHANNEL_ADMINS:
        if not is_admin or not meta.channel_id:
            await post_ephemeral(
                client,
                channel_id=meta.channel_id or user_id,
                user_id=user_id,
                text=CHANNEL_ADMINS_NEED_ADMIN_MESSAGE,
            )
            return
        async with runtime.sessionmaker() as session:
            grant = await get_channel_admins(
                session, tenant_id=tenant_id, platform="slack", channel_id=meta.channel_id
            )
        try:
            groups: dict[str, str] | None = await list_user_groups(client)
        except GroupLookupFailed:
            log.warning("slack.agent_setup.channel_admins.groups_unlisted", exc_info=True)
            groups = None
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=panel_views.build_channel_admins_form(
                meta=meta.with_view("channel_admins", root_view_id=view_id),
                user_ids=grant.user_ids if grant else (),
                group_ids=grant.role_ids if grant else (),
                groups=groups,
            ),
        )
        return

    if action_id == panel_views.ACTION_CHANNEL_SKILLS:
        if not is_admin or not meta.channel_id:
            await record_panel_write(
                runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                op="channel_skills",
                outcome="denied",
                reason="needs_admin",
            )
            await post_ephemeral(
                client,
                channel_id=meta.channel_id or user_id,
                user_id=user_id,
                text=CHANNEL_SKILLS_NEED_ADMIN_MESSAGE,
            )
            return
        async with runtime.sessionmaker() as session:
            rows = await list_channel_skills(
                session, tenant_id=tenant_id, platform="slack", channel_id=meta.channel_id
            )
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=panel_views.build_channel_skills_form(
                meta=meta.with_view("channel_skills", root_view_id=view_id), rows=rows
            ),
        )
        return

    if action_id == panel_views.ACTION_OPERATOR_MINT:
        if not is_admin:
            await record_panel_write(
                runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                op="operator_token_mint",
                outcome="denied",
                reason="needs_admin",
                token_kind="operator",
            )
            await post_ephemeral(
                client,
                channel_id=meta.channel_id or user_id,
                user_id=user_id,
                text=OPERATOR_TOKENS_NEED_ADMIN_MESSAGE,
            )
            return
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=panel_views.build_operator_token_form(
                meta=meta.with_view("operator_token", root_view_id=view_id)
            ),
        )
        return

    if action_id == panel_views.ACTION_OPERATOR_REVOKE:
        # Lazy import: that module refreshes the routing view built here.
        from daimon.adapters.slack.agent_setup.operator_tokens import (
            handle_operator_token_revoke,
        )

        picked: dict[str, Any] = action.get("selected_option") or {}
        try:
            jti = uuid.UUID(str(picked.get("value") or ""))
        except ValueError:
            return
        await handle_operator_token_revoke(
            runtime, client, meta=meta, user_id=user_id, is_admin=is_admin, jti=jti, view_id=view_id
        )
        return

    if action_id == panel_views.ACTION_ENVIRONMENT:
        selected: dict[str, Any] = action.get("selected_option") or {}
        value = str(selected.get("value") or "")
        if not value or not meta.channel_id:
            return
        subject = await load_picker_subject(
            runtime, client, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
        )
        if not await may_pick_environment(
            runtime, tenant_id=tenant_id, channel_id=meta.channel_id, subject=subject
        ):
            await post_ephemeral(
                client,
                channel_id=meta.channel_id,
                user_id=user_id,
                text=ENVIRONMENT_NEED_ADMIN_MESSAGE,
            )
            await record_panel_write(
                runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                op="environment",
                outcome="denied",
                reason="needs_admin",
            )
            return
        note = await save_environment_choice(
            runtime,
            tenant_id=tenant_id,
            channel_id=meta.channel_id,
            user_id=user_id,
            subject=subject,
            value=value,
        )
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,
            view=await load_routing_view(
                runtime, client, tenant_id=tenant_id, meta=meta, is_admin=is_admin, user_id=user_id
            ),
        )
        await post_ephemeral(client, channel_id=meta.channel_id, user_id=user_id, text=note)
        return

    if action_id in _RULE_ACTIONS:
        option = cast("Mapping[str, object]", action.get("selected_option") or {})
        value = str(option.get("value") or "")
        if not is_admin or not meta.channel_id:
            text = RULE_NEED_ADMIN_MESSAGE
            await record_panel_write(
                runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                op="channel_rule",
                outcome="denied",
                reason="needs_admin",
            )
        else:
            text = await change_rule(
                runtime,
                client,
                tenant_id=tenant_id,
                user_id=user_id,
                channel_id=meta.channel_id,
                readers=as_readers(value)
                if action_id == panel_views.ACTION_RULE_READERS
                else "own"
                if action_id == panel_views.ACTION_RULE_COPY
                else None,
                writers=as_writers(value) if action_id == panel_views.ACTION_RULE_WRITERS else None,
                copy=action_id == panel_views.ACTION_RULE_COPY,
                release=action_id == panel_views.ACTION_RULE_RELEASE,
            )
        await post_ephemeral(
            client, channel_id=meta.channel_id or user_id, user_id=user_id, text=text
        )
        if is_admin and view_id:
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=await load_routing_view(
                    runtime, client, tenant_id=tenant_id, meta=meta, is_admin=True
                ),
            )
        return

    if action_id == panel_views.ACTION_ADD_SKILL:
        agent_name = str(action.get("value") or meta.agent_name or "")
        if not agent_name or await refuse_unless_pin_allows(
            runtime,
            client,
            tenant_id=tenant_id,
            agent_name=agent_name,
            channel_id=meta.channel_id,
            user_id=user_id,
            is_admin=is_admin,
        ):
            return
        async with runtime.sessionmaker.begin() as session:
            principal = await get_or_create_platform_principal(
                session, platform="slack", external_id=user_id, tenant_id=tenant_id
            )
        if await refuse_unless_allowed_for_agent_name(
            runtime,
            client,
            operation="skill_add",
            tenant_id=tenant_id,
            agent_name=agent_name,
            channel_id=meta.channel_id,
            user_id=user_id,
            caller_account_id=principal.account_id,
        ):
            return
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=panel_views.build_add_skill_form(
                meta=meta.with_view("add_skill", agent_name=agent_name, root_view_id=view_id),
                files_in_chat=runtime.settings.tool_safety.enabled,
            ),
        )
        return

    if action_id == panel_views.ACTION_CODING_TOOLS:
        await handle_coding_tools_click(
            runtime,
            client,
            team_id=team_id,
            tenant_id=tenant_id,
            agent_name=str(action.get("value") or meta.agent_name or ""),
            channel_id=meta.channel_id,
            user_id=user_id,
            trigger_id=trigger_id,
        )
        return


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
    # Every panel view carries its identifiers in typed metadata. A click from
    # outside a view — the posted setup button, the coding-tools ephemeral —
    # decodes to None and takes its team and channel from the payload instead.
    panel_meta = decode_panel_metadata(str(view_info.get("private_metadata") or ""))

    team_id = team_id or (panel_meta.team_id if panel_meta else "")
    channel_info: dict[str, Any] = payload.get("channel") or {}
    channel_id = str((panel_meta.channel_id if panel_meta else "") or channel_info.get("id") or "")

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

        # Revoke is the one panel action clicked on an ephemeral rather than
        # inside a view, so it carries no panel metadata and is routed first.
        if action_id == panel_views.ACTION_REVOKE_TOKEN:
            jti = _parse_uuid(str(action.get("value") or ""))
            if jti is None:
                log.info("slack.agent_setup.revoke.unreadable_token_id")
                return
            await handle_revoke_token_click(
                runtime,
                client,
                tenant_id=tenant_id,
                jti=jti,
                user_id=user_id,
                response_url=str(payload.get("response_url") or ""),
            )
            return

        # A panel action without panel metadata is a click on a view this
        # deployment no longer serves; it falls through to the debug log.
        if panel_meta is not None and action_id in PANEL_ACTION_IDS:
            await _dispatch_panel_action(
                runtime,
                client,
                payload,
                action=action,
                action_id=action_id,
                meta=panel_meta,
                tenant_id=tenant_id,
                team_id=team_id,
                user_id=user_id,
            )
            return

        if action_id == "agent_setup__conversation":
            target_id = str(action.get("value") or "choose")
            link = await create_setup_conversation(
                runtime,
                client,
                team_id=team_id,
                channel_id=channel_id,
                user_id=user_id,
                target_ma_agent_id=None if target_id == "choose" else target_id,
            )
            handoff_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Your setup conversation is ready. Open it to reply to Daimon.",
                    },
                },
                setup_reply_button(link),
            ]
            if view_id:
                await client.views_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                    view_id=view_id,
                    view={
                        "type": "modal",
                        "title": {"type": "plain_text", "text": "Setup ready"},
                        "close": {"type": "plain_text", "text": "Close"},
                        "blocks": handoff_blocks,
                    },
                )
            else:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                    channel=channel_id,
                    user=user_id,
                    text=f"<{link}|Reply to Daimon in your setup conversation>",
                    blocks=handoff_blocks,
                )
            return

        log.debug(
            "slack.agent_setup_action.unknown_action_id",
            action_id=action_id,
            team_id=team_id,
        )

    except (
        DaimonError,
        anthropic.APIError,
        SlackApiError,
        InvalidToken,
        SQLAlchemyError,
        aiohttp.ClientError,
        TimeoutError,
    ) as exc:
        log.error(
            "slack.agent_setup_action_failed",
            team_id=team_id,
            action_id=action_id,
            exc_info=exc,
        )
        capture_exception_with_scope(exc)
        # A failed click is never silent. With a view on screen the failure
        # replaces its content, so the modal cannot sit on a stale render that
        # looks like the click did nothing; without one (a revoke click on an
        # ephemeral) the only place left to say so is an ephemeral.
        request_id = _new_request_id()
        if view_id:
            with contextlib.suppress(SlackApiError):
                await client.views_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                    view_id=view_id,
                    view=build_error_view(request_id=request_id),
                )
        elif channel_id or user_id:
            with contextlib.suppress(SlackApiError):
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                    channel=channel_id or user_id,
                    user=user_id,
                    text=render_error(exc, request_id=request_id),
                )

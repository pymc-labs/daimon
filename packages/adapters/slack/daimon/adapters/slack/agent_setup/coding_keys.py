"""Coding keys on Slack: an agent's live coding-tool keys, listed with a Revoke button each.

A workspace admin sees and revokes every key of the agent; the agent's owner sees
and revokes only their own; anyone else is told no. Both facts are read at click
time, on open and again on each Revoke, never from the rendered view. The list shows
who made a key, when it was made and ends, and whether it runs in one channel. It
never shows the token, which was shown once at mint.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Final, cast

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.panel_views import (
    ACTION_REVOKE_CODING_KEY,
    CODING_KEYS_LABEL,
)
from daimon.adapters.slack.agent_setup.read import load_panel_roster
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_ownership import owns_agent
from daimon.core.coding_keys import (
    CodingKeyAccess,
    CodingKeyEntry,
    coding_key_access,
    list_coding_keys,
    revoke_coding_key,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.roster import RosterAgent
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()

ACTION_BACK: Final = "agent_setup__coding_keys_back"
CALLBACK_CODING_KEYS: Final = "agent_setup__coding_keys_view"
CODING_KEYS_REFUSAL: Final = "Only an admin or this agent's owner can see its coding keys."
#: Keys shown at once: each takes a block of Slack's 100, and revoking one makes room.
MAX_SHOWN_KEYS: Final = 25


def _slack_date(moment: dt.datetime) -> str:
    return f"<!date^{int(moment.timestamp())}^{{date_short}}|{moment.date().isoformat()}>"


def key_line(entry: CodingKeyEntry) -> str:
    """One key as a line: who made it, when it was made and ends, where it runs."""
    maker = escape_mrkdwn(entry.creator) + (" (you)" if entry.mine else "")
    ends = f"ends {_slack_date(entry.expires_at)}" if entry.expires_at else "no end"
    where = "one channel only" if entry.bound else "any channel"
    return (
        f"*{maker}* made it {_slack_date(entry.created_at)}, {ends}, {where}. "
        f"Key {str(entry.jti)[:8]}"
    )


def build_view(
    meta: PanelMetadata, entries: list[CodingKeyEntry], *, access: CodingKeyAccess
) -> dict[str, Any]:
    """The coding keys modal: what this is, one row per key, and Back."""
    name = meta.agent_name or "Agent"
    whose = "Every" if access == "admin" else "Your"
    lines = [
        f"*Coding keys for {escape_mrkdwn(name)}*",
        f"{whose} live key made from Use from your coding tools. Revoke one to stop it.",
    ]
    if not entries:
        lines.append("No live keys.")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
    ]
    for entry in entries[:MAX_SHOWN_KEYS]:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": key_line(entry)},
                "accessory": {
                    "type": "button",
                    "action_id": ACTION_REVOKE_CODING_KEY,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Revoke"},
                    "value": str(entry.jti),
                },
            }
        )
    if len(entries) > MAX_SHOWN_KEYS:
        more = f"and {len(entries) - MAX_SHOWN_KEYS} more. Revoke one to see the next."
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": more}]})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_BACK,
                    "text": {"type": "plain_text", "text": "◀ Back"},
                }
            ],
        }
    )
    return finish_modal(
        title="Coding keys",
        blocks=blocks,
        private_metadata=encode_panel_metadata(meta.with_view("coding_keys")),
        callback_id=CALLBACK_CODING_KEYS,
    )


async def live_coding_key_access(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    team_id: str,
    user_id: str,
    agent: RosterAgent,
) -> tuple[CodingKeyAccess | None, uuid.UUID]:
    """What the clicker may do with this agent's coding keys, read now, and their account."""
    admin = await resolve_is_admin(client, user_id=user_id, host_team_id=team_id)
    async with runtime.sessionmaker.begin() as session:
        principal = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )
        owned = not admin and await owns_agent(
            session,
            tenant_id=tenant_id,
            ma_agent_id=agent.ma_agent_id,
            account_id=principal.account_id,
        )
    return coding_key_access(is_admin=admin, owns_agent=owned), principal.account_id


async def _target_agent(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, meta: PanelMetadata, name: str
) -> RosterAgent | None:
    async with runtime.sessionmaker() as session:
        roster = await load_panel_roster(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            channel_id=meta.channel_id or None,
            thread_id=None,
            default=runtime.deployment_default,
            is_admin=True,
        )
    return next((row for row in roster.rows if row.name == name), None)


async def _list(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    agent: RosterAgent,
    access: CodingKeyAccess,
    account_id: uuid.UUID,
) -> list[CodingKeyEntry]:
    async with runtime.sessionmaker() as session:
        return await list_coding_keys(
            session,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id),
            access=access,
            viewer_account_id=account_id,
            platform="slack",
            now=dt.datetime.now(dt.UTC),
        )


async def handle(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    payload: dict[str, Any],
    *,
    action: dict[str, Any],
    meta: PanelMetadata,
    team_id: str,
    user_id: str,
) -> bool:
    """Open, refresh, revoke from, or leave the coding keys list; False for any other action."""
    from daimon.adapters.slack.agent_setup import panel_views

    action_id = str(action.get("action_id") or "")
    if action_id not in (panel_views.ACTION_CODING_KEYS, ACTION_REVOKE_CODING_KEY, ACTION_BACK):
        return False
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    view_id = str(cast("dict[str, Any]", payload.get("view") or {}).get("id") or "")
    name = str(
        (action.get("value") if action_id == panel_views.ACTION_CODING_KEYS else None)
        or meta.agent_name
        or ""
    )
    if action_id == ACTION_BACK:
        await _back_to_details(
            runtime, client, tenant_id=tenant_id, team_id=team_id, user_id=user_id,
            meta=meta, name=name, view_id=view_id,
        )  # fmt: skip
        return True
    agent = await _target_agent(runtime, tenant_id=tenant_id, meta=meta, name=name)
    if agent is None:
        return True
    access, account_id = await live_coding_key_access(
        runtime, client, tenant_id=tenant_id, team_id=team_id, user_id=user_id, agent=agent
    )
    if access is None:
        await post_ephemeral(
            client,
            channel_id=meta.channel_id or user_id,
            user_id=user_id,
            text=CODING_KEYS_REFUSAL,
            error=True,
        )
        if action_id == ACTION_REVOKE_CODING_KEY:
            await _audit(runtime, tenant_id, user_id, _jti(action), "denied", "not_allowed")
        return True
    if action_id == ACTION_REVOKE_CODING_KEY:
        await _revoke(
            runtime, client, tenant_id=tenant_id, user_id=user_id, meta=meta,
            channel_id=meta.channel_id, agent=agent, access=access, account_id=account_id,
            action=action,
        )  # fmt: skip
    entries = await _list(
        runtime, tenant_id=tenant_id, agent=agent, access=access, account_id=account_id
    )
    view = build_view(meta.with_view("coding_keys", agent_name=name), entries, access=access)
    if action_id == panel_views.ACTION_CODING_KEYS:
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=str(payload.get("trigger_id") or ""), view=view
        )
    else:
        await client.views_update(view_id=view_id, view=view)  # pyright: ignore[reportUnknownMemberType]
    return True


def _jti(action: dict[str, Any]) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(action.get("value") or ""))
    except ValueError:
        return None


async def _revoke(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    meta: PanelMetadata,
    channel_id: str,
    agent: RosterAgent,
    access: CodingKeyAccess,
    account_id: uuid.UUID,
    action: dict[str, Any],
) -> None:
    jti = _jti(action)
    revoked = False
    if jti is not None:
        async with runtime.sessionmaker.begin() as session:
            revoked = await revoke_coding_key(
                session,
                jti=jti,
                tenant_id=tenant_id,
                agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id),
                access=access,
                viewer_account_id=account_id,
                now=dt.datetime.now(dt.UTC),
            )
    if revoked:
        log.info("slack.coding_keys.revoked", jti=str(jti))
        await _audit(runtime, tenant_id, user_id, jti, "allowed", "completed")
        return
    await _audit(runtime, tenant_id, user_id, jti, "error", "not_revocable")
    await post_ephemeral(
        client,
        channel_id=channel_id or user_id,
        user_id=user_id,
        text="That key was already revoked.",
        error=True,
    )


async def _back_to_details(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    team_id: str,
    user_id: str,
    meta: PanelMetadata,
    name: str,
    view_id: str,
) -> None:
    from daimon.adapters.slack.agent_setup.actions import load_details_view

    if not name:
        return
    view = await load_details_view(
        runtime,
        tenant_id=tenant_id,
        meta=meta,
        agent_name=name,
        is_admin=await resolve_is_admin(client, user_id=user_id, host_team_id=team_id),
        viewer_user_id=user_id,
    )
    if view is not None:
        await client.views_update(view_id=view_id, view=view)  # pyright: ignore[reportUnknownMemberType]


async def _audit(
    runtime: SlackRuntime,
    tenant_id: uuid.UUID,
    user_id: str,
    jti: uuid.UUID | None,
    outcome: PanelOutcome,
    reason: str,
) -> None:
    await record_panel_write(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id=user_id,
        op="coding_token_revoke",
        outcome=outcome,
        reason=reason,
        token_kind="agent",
        token_jti=jti,
    )


__all__ = ["CODING_KEYS_LABEL", "CODING_KEYS_REFUSAL", "handle"]

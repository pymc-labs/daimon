"""Use an agent from your coding tools: mint, render, revoke.

The Details view's 🧰 button posts an ephemeral carrying a per-agent MCP token
— the `claude mcp add` one-liner, the `.mcp.json` snippet, and a Revoke button
— while the modal stays open. Matching Discord's `mcp_access`, the token is
shown exactly once: it is never logged, never written to channel history, and
never leaves the ephemeral.

Minting is admin-gated and the gate is re-resolved after the ack, never read
from the rendered view: the button is visible to every member (hiding is not
gating) and a member's click gets an explanation instead of silence. Revoking
is limited to the account that minted the token. Pressed in a sealed channel,
or in one the agent is pinned to, the token is bound to that channel
(`coding_token_channel`) and its calls run there.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Final

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.panel_views import ACTION_REVOKE_TOKEN
from daimon.adapters.slack.agent_setup.read import (
    coding_tools_available,
    load_panel_roster,
    public_mcp_url,
)
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL
from daimon.core.authz import AgentRef, build_agent_ref
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import (
    coding_token_channel,
    coding_tool_config,
    mint_agent_mcp_token,
    token_jti,
)
from daimon.core.roster import RosterAgent
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.mcp_tokens import get_mcp_token, revoke_mcp_token
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "handle_coding_tools_click",
    "handle_revoke_token_click",
    "render_coding_tools_message",
]

log = structlog.get_logger()

NOT_CONFIGURED_MESSAGE: Final[str] = (
    "This deployment is not set up for coding-tool access yet. Ask the operator to finish "
    "setup, then try again. Nothing was saved."
)

REVOKE_BUTTON_LABEL: Final[str] = "🗑 Revoke this token"

TOKEN_REVOKED_MESSAGE: Final[str] = "Token revoked."


def _needs_admin_message(agent_name: str) -> str:
    return (
        f"Minting an access token for {agent_name} needs a workspace admin. "
        "Ask an admin to open Details and use this button."
    )


def render_coding_tools_message(
    *,
    agent_name: str,
    public_url: str,
    jwt: str,
    jti: uuid.UUID,
    channel_id: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build the ephemeral's fallback text and blocks for one minted token.

    Same two artifacts as Discord's `render_mcp_config`, each in its own fenced
    block so one tap copies exactly that block: the CLI one-liner first (most
    people just run it), then the `.mcp.json` snippet. The key name is
    `daimon-<agent-name>` so several agents namespace cleanly in one config
    file.

    The fallback text deliberately carries no token — it is what Slack shows in
    a notification preview. `channel_id` names the channel a bound token runs in.
    """
    cli_oneliner, mcp_json_block = coding_tool_config(
        agent_name=agent_name, public_url=public_url, jwt=jwt
    )
    bound = (
        f"\nIt runs in <#{channel_id}>, under that channel's pins, seal and budget."
        if channel_id is not None
        else ""
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Use `{escape_mrkdwn(agent_name)}` from your coding tools* — "
                    f"token shown once, copy it now.{bound}"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Run this:*\n```\n{cli_oneliner}\n```"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Or paste into `.mcp.json`:*\n```\n{mcp_json_block}\n```",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_REVOKE_TOKEN,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": REVOKE_BUTTON_LABEL, "emoji": True},
                    "value": str(jti),
                }
            ],
        },
    ]
    text = f"Use {agent_name} from your coding tools — token shown once, copy it now."
    return text, blocks


async def handle_coding_tools_click(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    tenant_id: uuid.UUID,
    agent_name: str,
    channel_id: str,
    user_id: str,
    trigger_id: str,
) -> None:
    """Mint a per-agent MCP token and post it to the clicker, modal untouched.

    `trigger_id` is accepted and unused on purpose: this reply is an ephemeral
    rather than a pushed view, so the open Details modal stays where it is.
    """
    del trigger_id  # the reply is an ephemeral; the modal stack is not touched
    public_url = public_mcp_url(runtime)
    jwt_secret = runtime.settings.mcp.jwt_secret
    if not coding_tools_available(runtime) or public_url is None or jwt_secret is None:
        await post_ephemeral(
            client,
            channel_id=channel_id or user_id,
            user_id=user_id,
            text=NOT_CONFIGURED_MESSAGE,
        )
        return

    if not await resolve_is_admin(client, user_id=user_id):
        log.info("slack.coding_tools.refused_non_admin", team_id=team_id, agent_name=agent_name)
        await post_ephemeral(
            client,
            channel_id=channel_id or user_id,
            user_id=user_id,
            text=_needs_admin_message(agent_name),
        )
        return

    async with runtime.sessionmaker() as session:
        roster = await load_panel_roster(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            channel_id=channel_id or None,
            thread_id=None,
            default=runtime.deployment_default,
        )
    target = next((row for row in roster.rows if row.name == agent_name), None)
    if target is None:
        await post_ephemeral(
            client,
            channel_id=channel_id or user_id,
            user_id=user_id,
            text=(
                f"`{agent_name}` is no longer available — it may have been deleted. "
                "Reopen /agent-setup for the current list."
            ),
        )
        return

    try:
        bound_channel_id = await _bound_channel(
            runtime, tenant_id=tenant_id, channel_id=channel_id or None, target=target
        )
    except AccessPolicyUnreadable:
        await post_ephemeral(
            client,
            channel_id=channel_id or user_id,
            user_id=user_id,
            text=POLICY_UNREADABLE_REFUSAL,
        )
        return
    account_id = await _resolve_actor_account_id(runtime, tenant_id=tenant_id, user_id=user_id)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=target.ma_agent_id)
    async with runtime.sessionmaker() as session, session.begin():
        token = await mint_agent_mcp_token(
            session,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            label=agent_name,
            secret=jwt_secret.get_secret_value().encode(),
            now=dt.datetime.now(dt.UTC),
            platform="slack" if bound_channel_id is not None else None,
            channel_id=bound_channel_id,
        )
    jti = token_jti(token)
    log.info(
        "slack.coding_tools.minted",
        team_id=team_id,
        agent_name=agent_name,
        jti=str(jti),
        bound_channel_id=bound_channel_id,
        # The token value itself is never logged.
    )

    text, blocks = render_coding_tools_message(
        agent_name=agent_name,
        public_url=public_url,
        jwt=token,
        jti=jti,
        channel_id=bound_channel_id,
    )
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel=channel_id or user_id,
        user=user_id,
        text=text,
        blocks=blocks,
    )


async def handle_revoke_token_click(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    jti: uuid.UUID,
    user_id: str,
    response_url: str,
) -> None:
    """Revoke a token for the account that minted it, and say so in place.

    `client` is accepted for signature parity with the other handlers; every
    reply here goes back through `response_url`, which addresses the ephemeral
    the button lives on without needing a channel id.
    """
    del client  # every reply on this path is addressed by response_url
    account_id = await _resolve_actor_account_id(runtime, tenant_id=tenant_id, user_id=user_id)
    async with runtime.sessionmaker() as session:
        row = await get_mcp_token(session, jti=jti)
    if row is None or row.tenant_id != tenant_id or row.account_id != account_id:
        log.info("slack.coding_tools.revoke_refused", jti=str(jti))
        await _respond(
            runtime,
            response_url,
            text="Only the person who minted this token can revoke it.",
            replace_original=False,
        )
        return

    async with runtime.sessionmaker() as session, session.begin():
        revoked = await revoke_mcp_token(session, jti=jti, now=dt.datetime.now(dt.UTC))
    if revoked is None:
        await _respond(
            runtime,
            response_url,
            text="That token was already revoked.",
            replace_original=True,
        )
        return
    log.info("slack.coding_tools.revoked", jti=str(jti))
    await _respond(runtime, response_url, text=TOKEN_REVOKED_MESSAGE, replace_original=True)


async def _bound_channel(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, channel_id: str | None, target: RosterAgent
) -> str | None:
    """The channel a token minted here is bound to; the agent is read only under a pin."""
    async with runtime.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    agent = AgentRef.of(target.name)
    if channel_id is not None and policy.agent_channel_pins:
        ma_agent = await runtime.anthropic.beta.agents.retrieve(target.ma_agent_id)
        agent = build_agent_ref(ma_agent.name, ma_agent.metadata, target.name)
    return coding_token_channel(policy, agent=agent, channel_id=channel_id)


async def _respond(
    runtime: SlackRuntime, response_url: str, *, text: str, replace_original: bool
) -> None:
    """Answer the click on its own message via Slack's response_url."""
    if not response_url:
        return
    await runtime.http_client.post(
        response_url,
        json={"replace_original": replace_original, "text": text},
    )


async def _resolve_actor_account_id(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, user_id: str
) -> uuid.UUID:
    """The clicker's account, minted on first sight, for attribution."""
    async with runtime.sessionmaker() as session, session.begin():
        principal = await get_or_create_platform_principal(
            session,
            platform="slack",
            external_id=user_id,
            tenant_id=tenant_id,
        )
    return principal.account_id

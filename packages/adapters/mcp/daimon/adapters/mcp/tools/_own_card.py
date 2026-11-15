"""Post a card Daimon answers itself (a host call or a provider switch) into the turn's thread.

One poster for every platform, so the call gate and the switch tool reach the same places
with the same visibility and write checks as the credential card.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import turn_origin_place
from daimon.core.access_policy import DM_SCOPE_PREFIX
from daimon.core.posted_controls import PostedCard
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError


async def post_own_card(
    runtime: McpRuntime, auth: AuthIdentity, *, origin: TurnOriginRow, card: PostedCard
) -> str:
    """Post `card` into the verified origin's thread; the posted message's id."""
    place = turn_origin_place(origin)
    if origin.platform == "slack":
        from daimon.adapters.mcp.tools.slack._credential_button import post_slack_control_card

        return await post_slack_control_card(
            runtime,
            auth,
            channel_id=origin.parent_channel_id,
            # A /dm-me scope is not a Slack ts: the card goes to the top of the DM.
            thread_ts=None if origin.thread_id.startswith(DM_SCOPE_PREFIX) else origin.thread_id,
            card=card,
            origin=place,
        )
    if origin.platform == "teams":
        from daimon.adapters.mcp.tools.teams._send import post_teams_control_card

        if runtime.teams_client is None:
            raise ToolError("Teams tools are not configured on this server")
        return await post_teams_control_card(
            runtime, auth, thread_id=origin.thread_id, card=card, origin=place
        )
    if origin.platform == "discord":
        from daimon.adapters.mcp.tools.discord._switch_card import post_switch_card

        return await post_switch_card(
            runtime, auth, channel_id=origin.thread_id, card=card, origin=place
        )
    if origin.platform == "web":
        return await _notify_web(runtime, origin, card)
    raise ToolError("I can't post a confirmation card in this conversation. Nothing changed.")


async def _notify_web(runtime: McpRuntime, origin: TurnOriginRow, card: PostedCard) -> str:
    """Hand the card to the web process, which shows it in the person's chat."""
    from daimon.core.stores.web_notices import WebControlNotice, send_web_notice

    pressable = next((button.custom_id for button in card.buttons if button.custom_id), None)
    if pressable is None or pressable.count(":") != 2:
        raise ToolError("I can't post a confirmation card in this conversation. Nothing changed.")
    # "hcc:<id>": every button of the card shares it, and the press route checks it.
    card_id = pressable.rsplit(":", 1)[0]
    async with runtime.session_factory.begin() as session:
        await send_web_notice(
            session,
            WebControlNotice(
                thread_id=origin.thread_id,
                card_id=card_id,
                title=card.headline,
                details=card.facts,
                footer=card.footer,
            ),
        )
    return card_id

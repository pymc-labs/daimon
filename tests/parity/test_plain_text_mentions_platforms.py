"""A typed "@<bot name>" means the same thing on Discord, Slack and Teams.

Each adapter has its own pre-DB gate for a channel message that names the bot
as text: Discord's ``is_plain_text_mention_candidate``, Slack's
``is_plain_text_mention_event`` and Teams' ``parse_inbound(...).by_name``.
All three must agree on who qualifies (a person in a channel or thread, not a
bot, not a DM, not a message that already carries a real mention), and all
three must then read the same tenant switch. Turning the switch on and off is
exercised per adapter in its own tests; this file keeps them in step.
"""

from __future__ import annotations

from typing import Any

import pytest
from daimon.adapters.discord import bot as discord_bot
from daimon.adapters.discord.gating import is_plain_text_mention_candidate
from daimon.adapters.mcp.tools import plain_text_mentions as mcp_tool
from daimon.adapters.slack import app as slack_app
from daimon.adapters.slack.gating import is_plain_text_mention_event
from daimon.adapters.teams import app as teams_app
from daimon.adapters.teams.identity import TeamsInbound, parse_inbound
from daimon.core.stores import tenants
from microsoft_teams.api import MessageActivity

_BOT = "Daimon"
_ENTRA = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PERSON = "11111111-2222-3333-4444-555555555555"
_BOT_ID = "28:bot-app-id"
_CHANNEL = "19:general@thread.tacv2"


def _discord(text: str, *, bot: bool, dm: bool, mentioned: bool) -> bool:
    return is_plain_text_mention_candidate(
        author_is_bot=bot,
        author_is_webhook=False,
        bot_mentioned=mentioned,
        guild_id=None if dm else "guild-1",
        content=f"<@999> {text}" if mentioned else text,
        bot_names=(_BOT,),
    )


def _slack(text: str, *, bot: bool, dm: bool, mentioned: bool) -> bool:
    event: dict[str, Any] = {
        "type": "message",
        "channel_type": "im" if dm else "channel",
        "user": "U_PERSON",
        "text": f"<@U_BOT> {text}" if mentioned else text,
    }
    if bot:
        event["bot_id"] = "B_OTHER"
    # A real mention is caught later, once the bot user id is known; it also
    # arrives as app_mention, so only the typed form is judged here.
    return is_plain_text_mention_event(event, bot_names=(_BOT,)) and not mentioned


def _teams(text: str, *, bot: bool, dm: bool, mentioned: bool) -> bool:
    entities: list[dict[str, object]] = []
    if mentioned:
        text = f"<at>{_BOT}</at> {text}"
        entities.append(
            {"type": "mention", "text": f"<at>{_BOT}</at>", "mentioned": {"id": _BOT_ID}}
        )
    sender: dict[str, object] = (
        {"id": "28:other-bot"} if bot else {"id": f"29:{_PERSON}", "aadObjectId": _PERSON}
    )
    conversation_id = "a:personal-chat" if dm else f"{_CHANNEL};messageid=1"
    activity = MessageActivity.model_validate(
        {
            "type": "message",
            "id": "2",
            "channelId": "msteams",
            "serviceUrl": "https://smba.example/",
            "from": sender,
            "recipient": {"id": _BOT_ID, "name": _BOT},
            "conversation": {
                "id": conversation_id,
                "conversationType": "personal" if dm else "channel",
                "tenantId": _ENTRA,
                **({} if dm else {"isGroup": True}),
            },
            "text": text,
            "entities": entities,
            "channelData": {"tenant": {"id": _ENTRA}}
            | ({} if dm else {"channel": {"id": _CHANNEL}, "team": {"id": "19:team@thread.tacv2"}}),
        }
    )
    parsed = parse_inbound(activity, configured_tenant=_ENTRA, service_url=None)
    return isinstance(parsed, TeamsInbound) and parsed.by_name


_ADAPTERS = {"discord": _discord, "slack": _slack, "teams": _teams}


@pytest.mark.parametrize("platform", sorted(_ADAPTERS))
@pytest.mark.parametrize(
    ("text", "bot", "dm", "mentioned", "expected", "why"),
    [
        ("@Daimon what changed?", False, False, False, True, "a person typing the name"),
        ("thanks @daimon!", False, False, False, True, "any case, mid-sentence"),
        ("@Daimon what changed?", True, False, False, False, "a bot typing the name"),
        ("@Daimon what changed?", False, True, False, False, "a DM, which needs no mention"),
        ("@Daimon look", False, False, True, False, "a real mention, already a turn"),
        ("Daimon, what changed?", False, False, False, False, "the name without an @"),
        ("mail ops@daimon.example", False, False, False, False, "an email address"),
    ],
)
def test_every_adapter_agrees_on_who_qualifies(
    platform: str, text: str, bot: bool, dm: bool, mentioned: bool, expected: bool, why: str
) -> None:
    """The same message is a candidate on every platform, or on none."""
    qualifies = _ADAPTERS[platform](text, bot=bot, dm=dm, mentioned=mentioned)
    assert qualifies is expected, f"{platform}: {why} must {'' if expected else 'not '}qualify"


@pytest.mark.parametrize("module", [discord_bot, slack_app, teams_app], ids=lambda m: m.__name__)
def test_every_adapter_reads_the_same_tenant_switch(module: object) -> None:
    """A matching message is admitted only by the one per-tenant flag the admin tool writes."""
    assert getattr(module, "get_plain_text_mentions", None) is tenants.get_plain_text_mentions, (
        f"{module!r} must read tenants.plain_text_mentions through the shared store"
    )


def test_the_admin_tool_serves_all_three_platforms() -> None:
    """The switch can be set from any of the platforms that honour it."""
    assert {"discord", "slack", "teams"} == mcp_tool._CHAT_PLATFORMS, (  # pyright: ignore[reportPrivateUsage]
        "the set tool must accept exactly the platforms whose adapters read the flag"
    )

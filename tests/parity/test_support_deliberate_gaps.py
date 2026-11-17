"""Executable record of where Ask a person's menu differs between platforms.

Every platform confirms a picked option before sending it. Discord opens a
modal, which for an option with no step and no fields holds only its terms and
Discord's own Submit. A Slack modal keeps the option picked until Send. Teams
shows the option's card (a dialog's `continue`, or the typed form's Next).
Teams settles on the post itself and names the settler by name. Every other
rule (prices, fields, each field's character cap) is shared core. A stale admin
change names what is there now on the CLI and MCP; the web page reloads it instead.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.cli.commands.support import _stale  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.discord import support_settle as discord_settle
from daimon.adapters.discord.support_escalation import SupportOptionModal
from daimon.adapters.mcp.tools.support import _refused  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.slack import support_settle as slack_settle
from daimon.adapters.slack.support_menu_form import (
    OPTION_ACTION_ID,
    OPTION_BLOCK_ID,
    build_menu_modal,
    read_menu_form,
)
from daimon.adapters.teams import support_menu_card as teams_card
from daimon.adapters.teams.support import SupportCommand
from daimon.adapters.teams.support_settle import TeamsSupportSettle
from daimon.adapters.web.channels import channel_refusals
from daimon.core.errors import StaleChannelValue
from daimon.core.stores.support_rows import LimitView, SupportEscalationRow, SupportMenuRow
from daimon.core.support import (
    MAX_PARAGRAPH_CHARS,
    Balance,
    OptionSnapshot,
    option_value,
    parse_menu,
)
from daimon.core.support_changes import MENU_SETTING
from discord.ui import TextDisplay, TextInput
from fastapi import HTTPException
from microsoft_teams.cards import ChoiceSetInput, TextBlock
from microsoft_teams.cards import TextInput as TeamsTextInput

_MENU = parse_menu(
    {
        "name": "Support",
        "options": [
            {"key": "quick", "label": "Quick", "price": {"kind": "fixed", "amount": "1"}},
            {
                "key": "review",
                "label": "Review",
                "price": {"kind": "range", "min": "1", "max": "3"},
            },
            {
                "key": "long",
                "label": "Long",
                "price": {"kind": "fixed", "amount": "2"},
                "fields": [{"key": "what", "kind": "paragraph", "label": "What"}],
            },
        ],
    }
)


def _view() -> LimitView:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    menu = SupportMenuRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        key="support",
        name=_MENU.name,
        revision=1,
        options=_MENU.options,
        set_by_account_id=None,
        created_at=now,
        updated_at=now,
    )
    balance = Balance(shared=None, shared_used=Decimal(0), cap=None, person_used=Decimal(0))
    return LimitView(limit=None, person=None, menu=menu, balance=balance, state="active")


def _picked(key: str) -> dict[str, Any]:
    menu = build_menu_modal(
        channel_id="C1", message_ts="1.2", thread_ts="1.0", view=_view(), sealed=False, picked=key
    )
    option = _MENU.option(key)
    assert option is not None, f"{key} is on the menu"
    pick = {OPTION_ACTION_ID: {"selected_option": {"value": option_value(option)}}}
    return {**menu, "state": {"values": {OPTION_BLOCK_ID: pick}}}


def test_option_without_fields_when_picked_then_each_platform_confirms_its_own_way() -> None:
    """Discord cannot rename a modal's Submit, so its confirm is the terms above Submit."""
    slack = _picked("quick")
    assert slack["submit"], "Slack: the picked option keeps the modal's Send"
    inputs = [b for b in slack["blocks"] if b["type"] == "input"]
    assert [b["block_id"] for b in inputs] == [OPTION_BLOCK_ID], "Slack: nothing more to fill in"
    form = read_menu_form(slack)
    assert form.errors is None and form.choice is not None, "Slack: Send submits the pick"
    assert form.choice.option_key == "quick"
    discord = SupportOptionModal(runtime=MagicMock(), origin=MagicMock(), option=_MENU.options[0])
    assert [type(c) for c in discord.children] == [TextDisplay], (
        "Discord: the terms alone; packages/adapters/discord/tests/test_support_limits_flow.py "
        "proves Submit posts it"
    )
    teams = teams_card.option_body(_MENU.options[0])
    assert not [e for e in teams if isinstance(e, ChoiceSetInput | TeamsTextInput)], (
        "Teams: the option's card, terms only, above Send"
    )


async def test_paragraph_field_when_built_then_every_platform_caps_at_the_core_limit() -> None:
    slack = _picked("long")
    field = next(b for b in slack["blocks"] if b.get("block_id", "").startswith("support_field"))
    assert field["element"]["max_length"] == MAX_PARAGRAPH_CHARS
    discord = SupportOptionModal(
        runtime=MagicMock(),
        origin=MagicMock(),
        option=_MENU.options[2],
    )
    (what,) = discord.field_inputs.values()
    assert isinstance(what, TextInput) and what.max_length == MAX_PARAGRAPH_CHARS
    teams = teams_card.option_body(_MENU.options[2])
    [field] = [e for e in teams if isinstance(e, TeamsTextInput)]
    assert field.max_length == MAX_PARAGRAPH_CHARS, "Teams: under its 4000, the core limit"


def _pending() -> SupportEscalationRow:
    review = _MENU.options[1]
    return SupportEscalationRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        account_id=None,
        platform="teams",
        platform_user_id="u",
        channel_id="c",
        message_id="m",
        ma_session_id=None,
        note="Please look",
        delivered_at=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        charged_to="limit",
        option=OptionSnapshot(key=review.key, label=review.label, price=review.price),
        hold=Decimal(1),
        charge=None,
        status="pending",
    )


def test_menu_when_shown_then_teams_asks_for_the_option_alone_and_slack_rerenders() -> None:
    """A Teams card cannot change its own inputs: the option's fields come on a second card."""
    menu = teams_card.menu_body(_view(), sealed=False)
    inputs = [e.id for e in menu if isinstance(e, ChoiceSetInput | TeamsTextInput)]
    assert inputs == [teams_card.OPTION_INPUT], "Teams: the menu asks only for the option"
    form = teams_card.option_body(_MENU.options[2])
    assert [e.id for e in form if isinstance(e, TeamsTextInput)] == ["field_what"], (
        "Teams: Next shows the fields; packages/adapters/teams/tests/test_support_limits_flow.py"
    )
    slack = [b["block_id"] for b in _picked("long")["blocks"] if b["type"] == "input"]
    assert slack[0] == OPTION_BLOCK_ID and len(slack) == 2, "Slack: one modal, re-rendered"


def test_pending_post_when_built_then_teams_settles_on_it_and_others_open_a_modal() -> None:
    """Teams has no modal from a channel post's button; the charge input sits on the post."""
    lines = teams_card.request_facts(_pending(), menu=True, balance="1 of 2 credits left")
    card = teams_card.settle_card(_pending(), lines)
    post = card.model_dump(mode="json", by_alias=True, exclude_none=True)
    [charge] = [e for e in post["body"] if e["type"] == "Input.Number"]
    assert (charge["id"], charge["value"]) == (teams_card.CHARGE_INPUT, 1), "Teams: the hold"
    [buttons] = [e["actions"] for e in post["body"] if e["type"] == "ActionSet"]
    assert {b["type"] for b in buttons} == {"Action.Execute"}, "Teams: settled on the post"
    # The closed post is rebuilt from the stored request; a click's data is the sender's.
    assert all("post" not in b["data"] for b in buttons), "Teams: no lines ride along"
    slack = slack_settle.settle_modal(_pending(), channel_id="C1", message_ts="1.2")
    assert slack["type"] == "modal", "Slack: Set charge opens a modal"
    assert issubclass(discord_settle.SettleModal, discord.ui.Modal), "Discord: a modal too"


def test_closed_post_when_settled_then_teams_names_the_settler_by_stored_name() -> None:
    """A Teams card cannot mention by id: the settler's name is the one Teams last gave."""
    before = _pending()
    after = before.model_copy(update={"status": "settled", "charge": Decimal(2)})
    lines = teams_card.request_facts(before, menu=True, balance=None)
    closed = teams_card.closed_card(lines, before=before, after=after, by="Ada")
    texts = [e.text for e in closed.body or [] if isinstance(e, TextBlock)]
    assert "Charged 2 credits by Ada" in texts and "Pending: 1-3 credits" not in texts, texts
    assert "Please look" in texts, "Teams: the note comes from the request, not the post"
    teams = inspect.getsource(TeamsSupportSettle._settler)  # pyright: ignore[reportPrivateUsage]
    assert "get_user_names" in teams, "Teams: stored names, so a settler without one is unnamed"
    assert slack_settle._settler("U123") == "<@U123>", "Slack: a mention"  # pyright: ignore[reportPrivateUsage]
    assert discord_settle._settler("123") == "<@123>", "Discord: a mention"  # pyright: ignore[reportPrivateUsage]


def test_pending_request_when_admins_were_messaged_then_teams_still_posts_it() -> None:
    """Teams messages admins first; a pending request is posted anyway, where staff settle it."""
    deliver = inspect.getsource(SupportCommand._deliver)  # pyright: ignore[reportPrivateUsage]
    pending = deliver.index('if row.status == "pending":')
    assert pending < deliver.index("elif not delivered:"), "Teams: posted whatever the chats did"


def test_stale_admin_change_when_refused_then_cli_and_mcp_say_what_is_there_now() -> None:
    """The web answers 409 with the plain refusal: its page reloads the current value."""
    stale = StaleChannelValue(MENU_SETTING, {"revision": 2})
    assert str(_stale(stale)).endswith("Now: revision 2."), "CLI: the revision now"
    assert "Now: revision 2." in str(_refused(stale)), "MCP: the revision now"
    with pytest.raises(HTTPException) as web, channel_refusals():
        raise stale
    assert (web.value.status_code, web.value.detail) == (409, str(stale)), "web: plain 409"

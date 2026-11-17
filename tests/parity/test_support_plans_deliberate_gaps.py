"""Executable record of where Ask a person's plan menu differs between platforms.

Picking an option with no step and no fields sends the request at once on
Discord: its select answers with a deferral, no form. A Slack modal cannot
submit from a radio pick, so the option stays picked and Send sends it. Teams
shows the menu, then the option's card (a dialog's `continue`, or the typed
form's Next). Teams settles on the post itself and names the settler by name.
Every other rule (prices, fields, the 3000-character cap) is shared core.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import discord
from daimon.adapters.discord import support_settle as discord_settle
from daimon.adapters.discord.support_escalation import SupportMenuSelect, SupportOptionModal
from daimon.adapters.slack import support_settle as slack_settle
from daimon.adapters.slack.support_plan_form import (
    OPTION_ACTION_ID,
    OPTION_BLOCK_ID,
    build_plan_menu,
    read_plan_form,
)
from daimon.adapters.teams import support_plan_card as teams_card
from daimon.adapters.teams.support import SupportCommand
from daimon.adapters.teams.support_settle import TeamsSupportSettle
from daimon.core.stores.support_rows import PlanView, SupportCatalogRow, SupportEscalationRow
from daimon.core.support_plans import (
    MAX_PARAGRAPH_CHARS,
    Balance,
    OptionSnapshot,
    parse_catalog,
)
from discord.ui import TextInput
from microsoft_teams.cards import ChoiceSetInput, TextBlock
from microsoft_teams.cards import TextInput as TeamsTextInput

_MENU = parse_catalog(
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


def _view() -> PlanView:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    catalog = SupportCatalogRow(
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
    balance = Balance(pool=None, pool_used=Decimal(0), cap=None, person_used=Decimal(0))
    return PlanView(plan=None, catalog=catalog, balance=balance, state="active")


def _picked(key: str) -> dict[str, Any]:
    menu = build_plan_menu(
        channel_id="C1", message_ts="1.2", thread_ts="1.0", view=_view(), sealed=False, picked=key
    )
    pick = {OPTION_ACTION_ID: {"selected_option": {"value": key}}}
    return {**menu, "state": {"values": {OPTION_BLOCK_ID: pick}}}


def test_option_without_fields_when_picked_then_discord_sends_and_slack_waits_for_send() -> None:
    slack = _picked("quick")
    assert slack["submit"], "Slack: the picked option keeps the modal's Send"
    inputs = [b for b in slack["blocks"] if b["type"] == "input"]
    assert [b["block_id"] for b in inputs] == [OPTION_BLOCK_ID], "Slack: nothing more to fill in"
    form = read_plan_form(slack)
    assert form.errors is None and form.choice is not None, "Slack: Send submits the pick"
    assert form.choice.option_key == "quick"
    discord = inspect.getsource(SupportMenuSelect._pick)  # pyright: ignore[reportPrivateUsage]
    assert "if not option.fields and not isinstance(option.price, StepsPrice):" in discord, (
        "Discord: the pick itself submits; "
        "packages/adapters/discord/tests/test_support_plans_flow.py proves the post"
    )


async def test_paragraph_field_when_built_then_every_platform_caps_at_the_core_limit() -> None:
    slack = _picked("long")
    field = next(b for b in slack["blocks"] if b.get("block_id", "").startswith("support_field"))
    assert field["element"]["max_length"] == MAX_PARAGRAPH_CHARS
    discord = SupportOptionModal(
        runtime=MagicMock(),
        origin=MagicMock(),
        option=_MENU.options[2],
        revision=1,
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
        charged_to="plan",
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
        "Teams: Next shows the fields; packages/adapters/teams/tests/test_support_plans_flow.py"
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

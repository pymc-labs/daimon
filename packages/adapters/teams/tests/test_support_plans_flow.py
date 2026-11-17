"""Support plans on Teams: the menu, option forms, admin exemption and settling a post.

Real Postgres and the real SDK route, as `test_support.py`; the plan and menu are
set through the store, the request read back through it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from daimon.adapters.teams import card
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.support import NOTE_INPUT
from daimon.adapters.teams.support_plan_card import CHARGE_INPUT, SETTLE_VERB
from daimon.core import platform_names
from daimon.core.config import SupportSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import support_plans as plans
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.platform_names import get_user_names
from daimon.core.stores.support_escalation import get_request
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.support_plans import (
    CLIENT_NOT_NOTIFIED,
    EXEMPT_OFFER,
    NOT_ENOUGH_CREDITS,
    OPTIONS_CHANGED,
    SETTLE_ELSEWHERE,
    option_value,
    parse_catalog,
)
from daimon.core.windows import WindowBounds
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    USER_NAME,
    SentRequest,
    TeamsApiFake,
    assert_card_renders,
    assert_teams_accepts,
    build_teams_runtime,
    make_card_action,
    make_invoke,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
OPS = "19:ops@thread.tacv2"
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
D = Decimal
TWO = D(2)

MENU = parse_catalog(
    {
        "name": "Support",
        "options": [
            {"key": "quick", "label": "Quick", "price": {"kind": "fixed", "amount": "0.5"}},
            {
                "key": "review",
                "label": "Review",
                "price": {"kind": "range", "min": "1", "max": "3"},
            },
            {
                "key": "call",
                "label": "Call",
                "price": {
                    "kind": "steps",
                    "label": "Length",
                    "steps": [
                        {"label": "30 min", "amount": "0.5"},
                        {"label": "1 hour", "amount": "1"},
                    ],
                },
                "fields": [
                    {"key": "topic", "kind": "text", "label": "Topic"},
                    {
                        "key": "when",
                        "kind": "choice",
                        "label": "When",
                        "choices": ["Today", "This week"],
                        "required": False,
                    },
                ],
            },
        ],
    }
)


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    menu: bool = True,
    per_person: Decimal = TWO,
    ends_at: datetime | None = None,
) -> None:
    """A workspace plan for the provisioned tenant, on MENU unless `menu` is False."""
    async with factory() as session, session.begin():
        catalog = None
        if menu:
            catalog = await plans.set_catalog(
                session, tenant_id=TENANT, key="support", spec=MENU, set_by_account_id=None
            )
        window = WindowBounds("total", None, None)
        if ends_at is not None:
            window = WindowBounds("fixed", ends_at - timedelta(days=30), ends_at)
        await plans.set_plan(
            session,
            tenant_id=TENANT,
            platform="teams",
            scope="workspace",
            scope_id="",
            catalog_id=None if catalog is None else catalog.id,
            pool=None,
            per_person=per_person,
            window=window,
            set_by_account_id=None,
        )


def _value(key: str) -> str:
    """The choice value MENU shows for `key`."""
    option = MENU.option(key)
    assert option is not None, f"{key} is on the menu"
    return option_value(option)


def _running(
    factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake, *, admins: tuple[str, ...] = ()
) -> AbstractAsyncContextManager[TeamsHttpService]:
    runtime = build_teams_runtime(factory, admins=admins)
    runtime.settings.support = SupportSettings(teams_escalation_channel_id=OPS)
    return running_service(runtime, fake)


async def _delivered(service: TeamsHttpService) -> None:
    async with asyncio.timeout(10):
        while service.turns.in_flight:
            await asyncio.sleep(0)


async def _typed(service: TeamsHttpService, fake: TeamsApiFake) -> dict[str, Any]:
    """Type `support` in the 1:1 chat; the card it answers with."""
    seen = len(fake.activity_requests)
    await post_activity(service, make_message_activity(text="support"))
    async with asyncio.timeout(10):
        while len(fake.activity_requests) == seen:
            await asyncio.sleep(0.01)
    return _card(fake.activity_requests[seen])


def _card(request: SentRequest) -> dict[str, Any]:
    """The one card a recorded send carries."""
    [attachment] = cast("list[dict[str, Any]]", request.body["attachments"])
    return attachment["content"]


def _button(content: dict[str, Any]) -> dict[str, Any]:
    """The card's one button's data, as Action.Execute sends it."""
    [action_set] = [b for b in content["body"] if b["type"] == "ActionSet"]
    [action] = action_set["actions"]
    return action["data"]


async def _click(service: TeamsHttpService, data: dict[str, Any], **inputs: object) -> Any:
    """Click a `support` button with `inputs` filled in; the invoke's answer."""
    fields = {k: v for k, v in (data | inputs).items() if k not in ("action", "op")}
    answer = await post_activity(service, make_card_action("support", data["op"], **fields))
    await _delivered(service)
    return answer


def _posts(fake: TeamsApiFake, method: str = "POST") -> list[SentRequest]:
    return [r for r in fake.activity_requests if r.method == method and OPS in r.url]


async def _pending(service: TeamsHttpService, fake: TeamsApiFake) -> tuple[Any, dict[str, Any]]:
    """A pending Review request: what its sender was told and the support post's card."""
    picked = await _click(service, _button(await _typed(service, fake)), option=_value("review"))
    sent = await _click(service, _button(picked["value"]))
    [post] = _posts(fake)
    assert_teams_accepts(post)
    return sent, _card(post)


async def _row(factory: async_sessionmaker[AsyncSession], post: dict[str, Any]) -> Any:
    buttons = _settle_buttons(post)
    async with factory() as session:
        return await get_request(session, escalation_id=uuid.UUID(hex=buttons["set"]["request"]))


def _settle_buttons(post: dict[str, Any]) -> dict[str, dict[str, Any]]:
    [action_set] = [b for b in post["body"] if b["type"] == "ActionSet"]
    return {a["data"]["op"]: a["data"] for a in action_set["actions"]}


def _settle(
    data: dict[str, Any], *, post_id: str, channel: str = OPS, **inputs: object
) -> dict[str, object]:
    """A staff click on the support post `post_id`, in the channel `channel`."""
    action = {"type": "Action.Execute", "verb": SETTLE_VERB, "data": data | inputs}
    click = make_invoke("adaptiveCard/action", {"action": action}, user=OTHER_AAD_OBJECT_ID)
    tenant = {"id": ENTRA_TENANT_ID}
    click["conversation"] = {
        "id": f"{channel};messageid={post_id}",
        "conversationType": "channel",
        "tenantId": ENTRA_TENANT_ID,
    }
    click["channelData"] = {"tenant": tenant, "channel": {"id": channel}}
    click["replyToId"] = post_id
    return click


def _text(content: dict[str, Any]) -> str:
    return json.dumps(content, ensure_ascii=False)


async def test_support_when_plan_has_a_menu_then_shows_options_and_balance(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A typed `support` on a menu plan answers with the menu, its prices and the balance."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)

    assert_card_renders(menu)
    [choices] = [b for b in menu["body"] if b["type"] == "Input.ChoiceSet"]
    titles = [c["title"] for c in choices["choices"]]
    assert titles == ["Quick: 0.5 credits", "Review: 1-3 credits", "Call: 0.5-1 credits"], titles
    assert "2 of 2 credits left" in _text(menu), "the balance sits under the menu"
    assert [c["value"] for c in choices["choices"]] == [
        _value(k) for k in ("quick", "review", "call")
    ], "each value carries its option's terms"


async def test_pick_when_option_has_steps_and_fields_then_its_form_shows_them(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Next replaces the menu with the picked option's step and fields."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)
        picked = await _click(service, _button(menu), option=_value("call"))

    form = picked["value"]
    assert_card_renders(form)
    ids = [b["id"] for b in form["body"] if b["type"].startswith("Input.")]
    assert ids == ["step", "field_topic", "field_when"], f"the step and the fields: {ids}"
    assert _button(form)["option"] == _value("call"), "Send names the option and its terms"


async def test_pick_when_the_menu_changed_then_options_changed(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A pick on older terms ends the form instead of sending them."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)
        picked = await _click(service, _button(menu), option="call:older")

    shown = _text(picked["value"])
    assert OPTIONS_CHANGED in shown and "Input." not in shown, "no form to fill in"


async def test_pick_when_balance_cannot_cover_it_then_the_menu_says_why(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """An option the balance cannot cover comes back to the menu with the reason."""
    await _seed(db_session_factory, per_person=D("0.5"))
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)
        picked = await _click(service, _button(menu), option=_value("review"))

    assert NOT_ENOUGH_CREDITS in _text(picked["value"]), "the menu says why"
    assert _posts(teams_api_fake) == [], "nothing sent"


async def test_support_when_the_plan_ended_then_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A plan past its window offers no form and names the day it ended."""
    await _seed(db_session_factory, ends_at=datetime(2026, 1, 18, tzinfo=UTC))
    async with _running(db_session_factory, teams_api_fake) as service:
        notice = await _typed(service, teams_api_fake)

    assert "Support ended on 17 Jan." in _text(notice), _text(notice)
    assert "ActionSet" not in _text(notice), "no form to send"


async def test_send_when_option_has_no_fields_then_posts_pending_with_settle_controls(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A range option is held at its minimum and posted with a charge input and two buttons."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        sent, post = await _pending(service, teams_api_fake)

    assert _text(sent["value"]).count("We will confirm the charge.") == 1, "told it is pending"
    shown = _text(post)
    for line in ("Review: 1-3 credits", "Pending: 1-3 credits", "1 of 2 credits left"):
        assert line in shown, f"the post names its terms, status and balance: {line}"
    assert CLIENT_NOT_NOTIFIED in shown, "decision 8 beside the controls"
    [charge] = [b for b in post["body"] if b["type"] == "Input.Number"]
    assert (charge["id"], charge["value"]) == (CHARGE_INPUT, 1), "prefilled with the hold"
    assert set(_settle_buttons(post)) == {"set", "decline"}, "Set charge and Decline"
    row = await _row(db_session_factory, post)
    assert (row.status, row.hold, row.support_post_id) == ("pending", D(1), "m-2"), row


async def test_send_when_option_has_a_step_and_fields_then_charged_at_submit(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A step prices the request at once: a text post with nothing left to settle."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)
        form = (await _click(service, _button(menu), option=_value("call")))["value"]
        sent = await _click(service, _button(form), step="1", field_topic="Billing export")

    assert "We will confirm" not in _text(sent["value"]), "nothing pending"
    [post] = _posts(teams_api_fake)
    text = str(post.body["text"])
    for line in ("Call, 1 hour: 1 credit", "Charged 1 credit", "Topic: Billing export"):
        assert line in text, f"the terms, the charge and the answers: {line}"
    assert "attachments" not in post.body, "no controls"


async def test_send_when_a_field_is_missing_then_the_form_comes_back(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A missing answer shows the option's form again, the step kept, nothing recorded."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)
        form = (await _click(service, _button(menu), option=_value("call")))["value"]
        again = (await _click(service, _button(form), step="1"))["value"]

    [step] = [b for b in again["body"] if b.get("id") == "step"]
    assert step["value"] == "1", "the step stays picked"
    errors = [b["text"] for b in again["body"] if b.get("color") == "Attention"]
    assert len(errors) == 1 and "Topic" in errors[0], f"the reason tops the form: {errors}"
    assert _posts(teams_api_fake) == [], "nothing sent"


async def test_support_when_requester_is_admin_then_uses_no_credits(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A deployment admin's request is free: the form, the reply and the post say so."""
    await _seed(db_session_factory, menu=False)
    async with _running(db_session_factory, teams_api_fake, admins=(AAD_OBJECT_ID,)) as service:
        form = await _typed(service, teams_api_fake)
        sent = await _click(service, _button(form), note="help please")

    assert EXEMPT_OFFER in _text(form), "the form says it is free"
    assert "This used no credits." in _text(sent["value"]), "so does the reply"
    [post] = _posts(teams_api_fake)
    assert "No charge: an admin asked" in str(post.body["text"]), "and the post"


async def test_ask_a_person_when_plan_has_a_menu_then_the_dialog_continues_to_the_option(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Ask a person on an answer: the menu, then the option's card in the same dialog."""
    await _seed(db_session_factory)

    def ask(name: str, data: dict[str, Any]) -> dict[str, object]:
        return make_invoke(name, {"data": {"action": card.ASK_HUMAN_DIALOG} | data})

    fetch = make_invoke("task/fetch", {"data": {"dialog_id": card.ASK_HUMAN_DIALOG}})
    async with _running(db_session_factory, teams_api_fake) as service:
        opened = await post_activity(service, fetch)
        menu = opened["task"]["value"]["card"]["content"]
        [pick] = menu["actions"]
        nexted = await post_activity(
            service, ask("task/submit", pick["data"] | {"option": _value("call")})
        )
        form = nexted["task"]["value"]["card"]["content"]
        [send] = form["actions"]
        sent = await post_activity(
            service, ask("task/submit", send["data"] | {"step": "0", "field_topic": "Exports"})
        )
        await _delivered(service)

    assert_card_renders(menu)
    assert_card_renders(form)
    assert nexted["task"]["type"] == "continue", "the option's card, in the same dialog"
    assert (pick["data"]["stage"], send["data"]["stage"]) == ("pick", "send")
    assert sent["task"]["value"] == (
        "Request sent. Someone will reply.\nUsed 0.5 credits. 1.5 of 2 credits left."
    ), sent
    [post] = _posts(teams_api_fake)
    assert "Call, 30 min: 0.5 credits" in str(post.body["text"]), "charged at its step"


async def test_set_charge_when_in_range_then_charges_and_closes_the_post(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Set charge in the support channel charges, closes the post, and tells only the settler."""
    await _seed(db_session_factory)
    async with db_session_factory() as session, session.begin():
        # Staff who took turns have a principal; only they get a stored name.
        await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=OTHER_AAD_OBJECT_ID
        )
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        buttons = _settle_buttons(post)
        before = len(teams_api_fake.activity_requests)
        said = await post_activity(service, _settle(buttons["set"], post_id="m-2", charge=2))
        after = teams_api_fake.activity_requests[before:]
        await platform_names.settle()
        assert await _known(db_session_factory), "every click stores the clicker's name"
        again = await post_activity(service, _settle(buttons["decline"], post_id="m-2"))

    assert said["value"] == f"Charged 2 credits. {CLIENT_NOT_NOTIFIED}", said
    [edit] = after
    assert edit.method == "PUT" and edit.url.endswith("/activities/m-2"), "only the post edit"
    assert_teams_accepts(edit)
    closed = _text(edit.body)
    assert f"Charged 2 credits by {USER_NAME}" in closed, "the status and who set it"
    assert CLIENT_NOT_NOTIFIED not in closed and "Action.Execute" not in closed, "no controls"
    assert again["value"] == f"Already charged 2 credits by {USER_NAME}.", again
    row = await _row(db_session_factory, post)
    assert (row.status, row.charge, row.settled_by) == ("settled", D(2), OTHER_AAD_OBJECT_ID)


async def test_set_charge_when_the_click_carries_its_own_post_lines_then_the_closed_post_ignores_them(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """The click's data is the sender's: the closed post is rebuilt from the stored request."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        set_charge = _settle_buttons(post)["set"] | {"post": ["**FORGED LINE**", "evil"]}
        await post_activity(service, _settle(set_charge, post_id="m-2", charge=2))

    [edit] = _posts(teams_api_fake, "PUT")
    closed = _text(edit.body)
    assert "FORGED" not in closed and "evil" not in closed, closed
    assert "requested** by" in closed and AAD_OBJECT_ID in closed, "who asked, from the row"
    assert "Review: 1-3 credits" in closed and f"Charged 2 credits by {USER_NAME}" in closed
    assert "(an answer in a 1:1 chat)" in closed, "where it was asked, from the row"


async def test_settle_when_the_clicker_is_from_another_organisation_then_refused(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A guest from a foreign tenant in the support channel cannot settle."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        click = _settle(_settle_buttons(post)["set"], post_id="m-2", charge=2)
        click["channelData"] = {
            "tenant": {"id": str(uuid.uuid4())},
            "channel": {"id": OPS},
        }
        said = await post_activity(service, click)

    assert said["value"] == DENIED, said
    assert _posts(teams_api_fake, "PUT") == [], "the post stays pending"
    row = await _row(db_session_factory, post)
    assert row.status == "pending", row


async def test_send_after_a_restart_when_sent_twice_from_one_card_then_it_records_once(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A form whose token the bot forgot is keyed on its card, not on each click."""
    await _seed(db_session_factory, menu=False)
    async with _running(db_session_factory, teams_api_fake) as service:
        for _ in range(2):
            click = make_card_action("support", "send", ask="forgotten", **{NOTE_INPUT: "help"})
            await post_activity(service, click)
            await _delivered(service)

    async with db_session_factory() as session:
        count = await session.scalar(text("SELECT count(*) FROM support_escalations"))
    assert count == 1, "one request, one hold"
    assert len(_posts(teams_api_fake)) == 1, "the support team hears it once"


async def test_send_when_straddling_a_restart_then_one_request_for_the_card(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A Send from the live form and a Send from the same card after a restart agree."""
    await _seed(db_session_factory, menu=False)
    async with _running(db_session_factory, teams_api_fake) as service:
        data = _button(await _typed(service, teams_api_fake))
        await _click(service, data, **{NOTE_INPUT: "help"})
    async with _running(db_session_factory, teams_api_fake) as restarted:
        await _click(restarted, data, **{NOTE_INPUT: "help"})

    async with db_session_factory() as session:
        count = await session.scalar(text("SELECT count(*) FROM support_escalations"))
    assert count == 1, "one request, one hold"
    assert len(_posts(teams_api_fake)) == 1, "the support team hears it once"


async def _known(factory: async_sessionmaker[AsyncSession]) -> bool:
    """Whether the settler's name is stored, as every click stores it."""
    async with factory() as session:
        names = await get_user_names(
            session, tenant_id=TENANT, platform="teams", user_ids=(OTHER_AAD_OBJECT_ID,)
        )
    return OTHER_AAD_OBJECT_ID in names


async def test_set_charge_when_out_of_range_then_names_the_range(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """An amount over the balance names the range and leaves the post pending."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        set_charge = _settle_buttons(post)["set"]
        said = await post_activity(service, _settle(set_charge, post_id="m-2", charge=5))

    assert said["value"] == "Enter an amount from 1 to 2.", "capped by the balance"
    assert _posts(teams_api_fake, "PUT") == [], "the post stays pending"
    row: SupportEscalationRow = await _row(db_session_factory, post)
    assert row.status == "pending", row


async def test_decline_when_pending_then_releases_the_hold(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Decline frees the hold, closes the post and messages nobody else."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        decline = _settle_buttons(post)["decline"]
        said = await post_activity(service, _settle(decline, post_id="m-2"))

    assert said["value"] == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}", said
    [edit] = _posts(teams_api_fake, "PUT")
    assert f"Declined by {USER_NAME}" in _text(edit.body), "the post says who declined"
    row = await _row(db_session_factory, post)
    assert (row.status, row.charge, row.settled_by) == ("declined", D(0), OTHER_AAD_OBJECT_ID)


@pytest.mark.parametrize("channel", ["19:elsewhere@thread.tacv2", None], ids=["channel", "chat"])
async def test_settle_click_when_outside_the_support_channel_then_refuses(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    channel: str | None,
) -> None:
    """A forwarded or copied post settles nothing outside the support channel."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        decline = _settle_buttons(post)["decline"]
        click = (
            make_card_action(SETTLE_VERB, "decline", request=decline["request"])
            if channel is None
            else _settle(decline, post_id="m-2", channel=channel)
        )
        said = await post_activity(service, click)

    assert said["value"] == SETTLE_ELSEWHERE, said
    row = await _row(db_session_factory, post)
    assert row.status == "pending", "nothing settled"

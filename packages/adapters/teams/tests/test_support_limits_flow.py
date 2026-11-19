"""Support limits on Teams: the menu, option forms, admin exemption and settling a post.

Real Postgres and the real SDK route, as `test_support.py`; the limit and menu are
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
from daimon.adapters.teams.support_menu_card import CHARGE_INPUT, SETTLE_VERB
from daimon.core import platform_names
from daimon.core.config import SupportSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import support as store
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.platform_names import get_user_names
from daimon.core.stores.support_escalation import get_request, submit_request
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.support import (
    CLIENT_NOT_NOTIFIED,
    EXEMPT_OFFER,
    NOT_ENOUGH_CREDITS,
    OPTIONS_CHANGED,
    OWN_REQUEST,
    SETTLE_ELSEWHERE,
    MenuSpec,
    RequestChoice,
    SupportPlace,
    option_value,
    parse_menu,
)
from daimon.core.support_escalation import ALREADY_REQUESTED
from daimon.core.teams_bot_framework import SERVICE_URL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
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
OWN = "19:own@thread.tacv2"
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
D = Decimal
TWO = D(2)

MENU = parse_menu(
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
    spec: MenuSpec = MENU,
    per_person: Decimal = TWO,
    ends_at: datetime | None = None,
    limit: bool = True,
) -> None:
    """A workspace limit (unless `limit` is False) for the provisioned tenant, on `spec` unless `menu` is."""
    async with factory() as session, session.begin():
        if menu:
            row = await store.set_menu(
                session, tenant_id=TENANT, key="support", spec=spec, set_by_account_id=None
            )
            await store.set_menu_use(
                session,
                tenant_id=TENANT,
                scope="workspace",
                scope_id="",
                menu_id=row.id,
                set_by_account_id=None,
            )
        if limit:
            await store.set_limit(
                session,
                tenant_id=TENANT,
                scope="workspace",
                scope_id="",
                platform_user_id=None,
                shared=None,
                each_person=per_person,
                window="total",
                starts_at=None if ends_at is None else ends_at - timedelta(days=30),
                ends_at=ends_at,
                set_by_account_id=None,
            )


def _value(key: str) -> str:
    """The choice value MENU shows for `key`."""
    option = MENU.option(key)
    assert option is not None, f"{key} is on the menu"
    return option_value(option)


def _running(
    factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    *,
    admins: tuple[str, ...] = (),
    credits_per_user: int = 20,
) -> AbstractAsyncContextManager[TeamsHttpService]:
    runtime = build_teams_runtime(factory, admins=admins)
    runtime.settings.support = SupportSettings(
        teams_escalation_channel_id=OPS, credits_per_user=credits_per_user
    )
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


def _posts(fake: TeamsApiFake, method: str = "POST", channel: str = OPS) -> list[SentRequest]:
    return [r for r in fake.activity_requests if r.method == method and channel in r.url]


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
    data: dict[str, Any],
    *,
    post_id: str,
    channel: str = OPS,
    user: str = OTHER_AAD_OBJECT_ID,
    **inputs: object,
) -> dict[str, object]:
    """A click by `user` (staff unless named) on the support post `post_id`, in `channel`."""
    action = {"type": "Action.Execute", "verb": SETTLE_VERB, "data": data | inputs}
    click = make_invoke("adaptiveCard/action", {"action": action}, user=user)
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


async def test_support_when_limit_has_a_menu_then_shows_options_and_balance(
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


async def _asked_before(
    factory: async_sessionmaker[AsyncSession], key: str, *, message_id: str = "m-7"
) -> uuid.UUID:
    """The person already asked with `key` on `message_id` (the conftest card), via the ledger."""
    option = MENU.option(key)
    assert option is not None, f"{key} is on the menu"
    async with factory() as session, session.begin():
        outcome = await submit_request(
            session,
            tenant_id=TENANT,
            account_id=None,
            platform="teams",
            platform_user_id=AAD_OBJECT_ID,
            channel_id=CONVERSATION_ID,
            message_id=message_id,
            ma_session_id=None,
            place=SupportPlace(channel_id=CONVERSATION_ID, agent_id=None),
            choice=RequestChoice.of(option),
            default_allowance=20,
            exempt=False,
        )
    assert outcome.status == "recorded" and outcome.row is not None, outcome.status
    return outcome.row.id


async def test_support_when_nothing_is_affordable_then_the_refusal_and_balance_without_a_menu(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """With no option the balance covers, `support` says why and what is left: no menu."""
    await _seed(db_session_factory, per_person=D("0.5"))
    await _asked_before(db_session_factory, "quick", message_id="m-earlier")
    async with _running(db_session_factory, teams_api_fake) as service:
        notice = await _typed(service, teams_api_fake)

    shown = _text(notice)
    assert "Input.ChoiceSet" not in shown and "ActionSet" not in shown, "no menu"
    assert NOT_ENOUGH_CREDITS in shown and "0 of 0.5 credits left." in shown, shown


async def test_pick_when_already_asked_and_unaffordable_then_already_asked_not_credits(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A repeat of an option the balance no longer covers says already asked."""
    await _seed(db_session_factory, per_person=D("1.5"))
    await _asked_before(db_session_factory, "review")
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)
        picked = await _click(service, _button(menu), option=_value("review"))

    shown = _text(picked["value"])
    assert ALREADY_REQUESTED in shown and NOT_ENOUGH_CREDITS not in shown, shown
    assert _posts(teams_api_fake) == [], "nothing sent"


async def test_ask_a_person_when_already_asked_and_nothing_else_is_affordable_then_already_asked(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Ask a person again on the same answer at no credits: already asked, no menu."""
    await _seed(db_session_factory, per_person=D("0.5"))
    await _asked_before(db_session_factory, "quick")
    fetch = make_invoke("task/fetch", {"data": {"dialog_id": card.ASK_HUMAN_DIALOG}})
    async with _running(db_session_factory, teams_api_fake) as service:
        opened = await post_activity(service, fetch)

    shown = _text(opened["task"]["value"]["card"]["content"])
    assert ALREADY_REQUESTED in shown and "Input.ChoiceSet" not in shown, shown


async def test_support_when_the_limit_ended_then_says_so(
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


async def test_send_when_charged_to_the_default_then_the_open_and_closed_posts_agree(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A range option with no limit: facts and controls open, the same facts closed, no balance."""
    await _seed(db_session_factory, limit=False)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        before = len(teams_api_fake.activity_requests)
        buttons = _settle_buttons(post)
        await post_activity(service, _settle(buttons["set"], post_id="m-2", charge=2))
        [edit] = teams_api_fake.activity_requests[before:]

    shown, closed = _text(post), _text(edit.body)
    for line in ("Review: 1-3 credits", "Pending: 1-3 credits"):
        assert line in shown, f"the open post names its terms and price: {line}"
    assert "credits left" not in shown, "no limit, so no balance"
    assert CLIENT_NOT_NOTIFIED in shown, "the settler is told the client is not notified"
    assert set(_settle_buttons(post)) == {"set", "decline"}, "Set charge and Decline"
    assert "Review: 1-3 credits" in closed, "the closed post keeps the option's terms"
    assert "Charged 2 credits" in closed


async def test_send_when_every_field_is_full_then_the_post_shows_each_unclipped(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Four full paragraph fields reach the support post whole, each on its own block."""
    fields = [{"key": f"part{n}", "kind": "paragraph", "label": f"Part {n}"} for n in range(1, 5)]
    price = {"kind": "range", "min": "1", "max": "2"}
    spec = parse_menu(
        {
            "name": "Support",
            "options": [{"key": "brief", "label": "Brief", "price": price, "fields": fields}],
        }
    )
    await _seed(db_session_factory, spec=spec)
    answers = {f"field_part{n}": str(n) * 3000 for n in range(1, 5)}
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)
        picked = await _click(service, _button(menu), option=option_value(spec.options[0]))
        await _click(service, _button(picked["value"]), **answers)

    [post] = _posts(teams_api_fake)
    assert_teams_accepts(post)
    texts = [b["text"] for b in _card(post)["body"] if b["type"] == "TextBlock"]
    for n in range(1, 5):
        assert f"Part {n}: {str(n) * 3000}" in texts, f"part {n} whole, on its own block"


async def test_send_when_option_has_a_step_then_held_until_accepted_at_its_price(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A step waits for Accept, which charges its price with no amount to type."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        menu = await _typed(service, teams_api_fake)
        form = (await _click(service, _button(menu), option=_value("call")))["value"]
        sent = await _click(service, _button(form), step="1", field_topic="Billing export")
        [request] = _posts(teams_api_fake)
        assert_teams_accepts(request)
        post = _card(request)
        buttons = _settle_buttons(post)
        before = len(teams_api_fake.activity_requests)
        said = await post_activity(service, _settle(buttons["accept"], post_id="m-2"))
        [edit] = teams_api_fake.activity_requests[before:]

    assert "We will confirm the charge." in _text(sent["value"]), "told it is pending"
    for line in ("Call, 1 hour: 1 credit", "Pending: 1 credit", "Topic: Billing export"):
        assert line in _text(post), f"the terms, the hold and the answers: {line}"
    assert set(buttons) == {"accept", "decline"}, "Accept and Decline"
    assert not [b for b in post["body"] if b["type"] == "Input.Number"], "no amount to type"
    assert said["value"] == f"Charged 1 credit. {CLIENT_NOT_NOTIFIED}", said
    assert "Charged 1 credit" in _text(edit.body), "the post closes charged"


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


async def test_ask_a_person_when_limit_has_a_menu_then_the_dialog_continues_to_the_option(
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
        "Request sent. Someone will reply.\nHolding 0.5 credits. We will confirm the charge."
    ), sent
    [post] = _posts(teams_api_fake)
    assert "Call, 30 min: 0.5 credits" in _text(post.body), "held at its step"


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


async def test_settle_when_the_clicker_is_the_requester_then_refused_and_another_member_settles(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Teams spells an id the same way on the request and the click, so no one settles their own."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        decline = _settle_buttons(post)["decline"]
        own = await post_activity(
            service, _settle(decline, post_id="m-2", user=AAD_OBJECT_ID.upper())
        )
        pending = await _row(db_session_factory, post)
        other = await post_activity(service, _settle(decline, post_id="m-2"))

    assert own["value"] == OWN_REQUEST, own
    assert pending.status == "pending", "the requester's click settled nothing"
    assert other["value"].startswith("Declined."), other
    assert (await _row(db_session_factory, post)).status == "declined", "another member settles it"


async def test_settle_when_the_clicker_is_a_deployment_admin_requester_then_settles_own_request(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Submit exempts a deployment admin, so settle lets them settle their own request."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
    decline = _settle_buttons(post)["decline"]
    async with _running(db_session_factory, teams_api_fake, admins=(AAD_OBJECT_ID,)) as service:
        own = await post_activity(service, _settle(decline, post_id="m-2", user=AAD_OBJECT_ID))

    assert own["value"].startswith("Declined."), own
    assert (await _row(db_session_factory, post)).status == "declined", "settled by its requester"


async def test_decline_when_asking_was_turned_off_since_then_the_post_still_settles(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """With the default credits at 0 a post already in the channel keeps working buttons."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
    decline = _settle_buttons(post)["decline"]
    async with _running(db_session_factory, teams_api_fake, credits_per_user=0) as service:
        said = await post_activity(service, _settle(decline, post_id="m-2"))

    assert said["value"] == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}", said
    row = await _row(db_session_factory, post)
    assert row.status == "declined", row


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


async def _own_channel(factory: async_sessionmaker[AsyncSession]) -> None:
    """The workspace picks OWN as its request channel."""
    async with factory() as session, session.begin():
        await store.set_support_channel(
            session,
            tenant_id=TENANT,
            platform="teams",
            channel_id=OWN,
            teams_team_id="team-own",
            teams_service_url=SERVICE_URL,
            set_by_account_id=None,
        )


async def test_send_when_the_workspace_has_its_own_channel_then_posts_and_stamps_there(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A workspace's own channel takes its requests instead of the deployment's."""
    await _seed(db_session_factory)
    await _own_channel(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        picked = await _click(
            service, _button(await _typed(service, teams_api_fake)), option=_value("review")
        )
        await _click(service, _button(picked["value"]))

    assert _posts(teams_api_fake) == [], "nothing in the deployment channel"
    [post] = _posts(teams_api_fake, channel=OWN)
    row = await _row(db_session_factory, _card(post))
    assert row.support_channel_id == OWN and row.support_post_id, "stamped with the own channel"


async def test_send_when_own_channels_are_off_then_posts_in_the_deployment_channel(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """With `support_own_channel` off a picked channel is ignored."""
    await _seed(db_session_factory)
    await _own_channel(db_session_factory)
    runtime = build_teams_runtime(db_session_factory)
    runtime.settings.support = SupportSettings(teams_escalation_channel_id=OPS)
    runtime.settings.features = {**runtime.settings.features, "support_own_channel": "off"}
    async with running_service(runtime, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)

    assert _posts(teams_api_fake, channel=OWN) == [], "the own channel gets nothing"
    row = await _row(db_session_factory, post)
    assert row.support_channel_id == OPS, "stamped with the deployment channel"


async def test_settle_when_the_channel_changed_since_the_post_then_only_the_posted_channel_settles(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """An open request settles where it was posted, not in the newly picked channel."""
    await _seed(db_session_factory)
    async with _running(db_session_factory, teams_api_fake) as service:
        _sent, post = await _pending(service, teams_api_fake)
        await _own_channel(db_session_factory)
        decline = _settle_buttons(post)["decline"]
        moved = await post_activity(service, _settle(decline, post_id="m-2", channel=OWN))
        said = await post_activity(service, _settle(decline, post_id="m-2"))

    assert moved["value"] == SETTLE_ELSEWHERE, moved
    assert said["value"] == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}", said
    row = await _row(db_session_factory, post)
    assert row.status == "declined", row


@pytest.mark.parametrize(
    ("own", "channel", "settles"),
    [(False, OPS, True), (True, OWN, True), (True, OPS, False)],
    ids=["deployment", "own", "not-the-deployment-once-own"],
)
async def test_settle_when_the_request_has_no_stamp_then_its_default_channel_settles(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    own: bool,
    channel: str,
    settles: bool,
) -> None:
    """A request from before the stamp settles in the own channel, else the deployment's."""
    await _seed(db_session_factory)
    if own:
        await _own_channel(db_session_factory)
    request = await _asked_before(db_session_factory, "review")
    data = {"action": SETTLE_VERB, "op": "decline", "request": request.hex}
    async with _running(db_session_factory, teams_api_fake) as service:
        said = await post_activity(service, _settle(data, post_id="m-old", channel=channel))

    async with db_session_factory() as session:
        row = await get_request(session, escalation_id=request)
    assert row is not None, "the request exists"
    assert (row.status == "declined") is settles, row.status
    if not settles:
        assert said["value"] == SETTLE_ELSEWHERE, said

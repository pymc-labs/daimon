"""Taps on a post_wizard form: who may tap, what each writes, and the one submitted turn."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.wizard import VERB, TeamsWizards
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.wizard_session import create_wizard_session, get_wizard_session
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from microsoft_teams.api import AdaptiveCardInvokeActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    build_teams_runtime,
    make_card_action,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")
SHORT_ID = "abcd1234"
SPEC = WizardSpec(
    prompt="Order form",
    steps=[
        Step(
            key="color",
            question="Pick a color",
            kind=StepKind.CHOICE,
            options=[Option(label="Red", value="red"), Option(label="Blue", value="blue")],
        ),
        Step(
            key="toppings",
            question="Pick toppings",
            kind=StepKind.MULTI,
            options=[Option(label="Cheese", value="cheese"), Option(label="Olives", value="o")],
        ),
        Step(key="notes", question="Any notes?", kind=StepKind.TEXT),
    ],
)


async def _form(db_factory: async_sessionmaker[AsyncSession], *, step: int = 0) -> None:
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    now = datetime.now(UTC)
    async with db_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="teams", external_id=AAD_OBJECT_ID
        )
        await create_wizard_session(
            session,
            short_id=SHORT_ID,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            requester_platform_user_id=AAD_OBJECT_ID,
            channel_id=CONVERSATION_ID,
            message_id="m-7",  # the card message `make_invoke` replies to
            spec=SPEC.model_dump(mode="json"),
            answers={"color": ["red"]} if step else {},
            current_step=step,
            status="open",
            expires_at=now + timedelta(hours=1),
            now=now,
        )


def _tap(op: str, *, user: str = AAD_OBJECT_ID, **data: str) -> Any:
    activity = make_card_action(VERB, op, user=user, wz=SHORT_ID, **data)
    return SimpleNamespace(
        activity=AdaptiveCardInvokeActivity.model_validate(activity),
        conversation_ref=SimpleNamespace(service_url=SERVICE_URL),
    )


def _wizards(
    db_factory: async_sessionmaker[AsyncSession], *, draining: bool = False
) -> tuple[TeamsWizards, list[TeamsInbound]]:
    started: list[TeamsInbound] = []
    runtime = build_teams_runtime(db_factory)
    return TeamsWizards(runtime, start_turn=started.append, draining=lambda: draining), started


async def _row(db_factory: async_sessionmaker[AsyncSession]) -> Any:
    async with db_factory() as session:
        return await get_wizard_session(session, short_id=SHORT_ID)


async def test_a_choice_tap_saves_the_answer_and_shows_the_next_step(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _form(db_session_factory)
    wizards, _ = _wizards(db_session_factory)
    response = await wizards.on_action(_tap("s0_c1"))
    assert "Pick toppings" in response.model_dump_json(by_alias=True)
    row = await _row(db_session_factory)
    assert (row.answers, row.current_step) == ({"color": ["blue"]}, 1)


async def test_only_the_person_who_asked_may_tap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _form(db_session_factory)
    wizards, _ = _wizards(db_session_factory)
    response = await wizards.on_action(_tap("s0_c0", user=OTHER_AAD_OBJECT_ID))
    assert response.value == "This form was for someone else."
    assert (await _row(db_session_factory)).current_step == 0


async def test_checked_boxes_ride_the_next_button_as_indices(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _form(db_session_factory, step=1)
    wizards, _ = _wizards(db_session_factory)
    forged = await wizards.on_action(_tap("next", sel="s1_sel", values="0,9"))
    assert forged.value == "This form changed before your tap landed."
    await wizards.on_action(_tap("next", sel="s1_sel", values="1"))
    row = await _row(db_session_factory)
    assert (row.answers["toppings"], row.current_step) == (["o"], 2)


async def test_a_text_answer_must_be_typed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _form(db_session_factory, step=2)
    wizards, _ = _wizards(db_session_factory)
    assert (
        await wizards.on_action(_tap("s2_custom", text="  "))
    ).value == "Type your answer first."
    await wizards.on_action(_tap("s2_custom", text="extra cheese"))
    row = await _row(db_session_factory)
    assert (row.answers["notes"], row.current_step) == (["extra cheese"], 3)


async def test_submit_claims_once_and_starts_one_turn_with_the_answers(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _form(db_session_factory, step=3)
    wizards, started = _wizards(db_session_factory)
    await wizards.on_action(_tap("submit"))
    again = await wizards.on_action(_tap("submit"))
    assert again.value == "This form was already submitted."
    [inbound] = started
    assert (inbound.kind, inbound.conversation_id, inbound.user_id) == (
        "dm",
        CONVERSATION_ID,
        AAD_OBJECT_ID,
    )
    assert inbound.text.startswith("Wizard answers (Order form):") and '"red"' in inbound.text


async def test_a_draining_process_leaves_the_form_to_submit_later(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _form(db_session_factory, step=3)
    wizards, started = _wizards(db_session_factory, draining=True)
    response = await wizards.on_action(_tap("submit"))
    assert "restarting" in str(response.value) and started == []
    assert (await _row(db_session_factory)).status == "open"


@pytest.mark.usefixtures("entra_env", "stub_bot_token")
async def test_the_service_routes_wizard_taps(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: Any
) -> None:
    await _form(db_session_factory)
    runtime = build_teams_runtime(db_session_factory)
    async with running_service(runtime, teams_api_fake) as service:
        response = await post_activity(service, make_card_action(VERB, "s0_c0", wz=SHORT_ID))
    assert "Pick toppings" in str(response)

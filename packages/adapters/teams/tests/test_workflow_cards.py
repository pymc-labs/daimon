"""Workflow cards on Teams: the form and panel cards, and the run card edited in place."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

from daimon.adapters.teams import workflow_cards
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.config import WebSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.teams_bot_framework import SERVICE_URL
from daimon.core.workflows import chat_cards
from daimon.core.workflows.chat_cards import ChatPlace, RunForm
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import mock_settings
from daimon.testing.teams_surface import TeamsSurfaceHarness
from microsoft_teams.api import MessageActivityInput
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_the_follower_edits_the_run_card_in_its_conversation() -> None:
    harness = TeamsSurfaceHarness(
        evidence_id="wf", session_id="s", root_turn_id="r", service_url=SERVICE_URL
    )
    first = MessageActivityInput().add_card(workflow_cards.form_card(_form()))
    sent = await harness.sender.send("19:qa-thread", first, service_url=SERVICE_URL)
    edit = workflow_cards.card_editor(harness.sender)
    await edit(
        uuid.uuid4(), ChatPlace("teams", "19:qa-thread", sent.id), chat_cards.starting_card("Lead")
    )
    [shown] = harness.recorder.surviving_messages()
    assert shown.message_id == sent.id and "starting" in shown.payload_json


def _form() -> RunForm:
    spec: dict[str, Any] = {"name": "Lead", "inputs": {"properties": {"email": {}, "n": {}}}}
    form = chat_cards.run_form(uuid.uuid4(), spec, limit=workflow_cards.FORM_LIMIT)
    assert form is not None
    return form


def test_the_run_form_submits_its_own_id_with_each_field() -> None:
    form = _form()
    card = workflow_cards.form_card(form).model_dump(by_alias=True, exclude_none=True)
    assert [b.get("id") for b in card["body"][1:]] == ["email", "n"]
    assert card["actions"][0]["data"]["custom_id"] == form.submit_id


async def test_a_workflow_card_links_the_web_only_where_web_awareness_is_on(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = mock_settings()
    settings.web = WebSettings(public_url="https://app.example.com/")  # type: ignore[arg-type]
    runtime = cast(
        TeamsRuntime,
        SimpleNamespace(
            settings=settings,
            sessionmaker=db_session_factory,
            deployment_default=DeploymentDefault(),
            anthropic=None,
            resolver_cache=new_resolver_cache(),
        ),
    )
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        await set_feature_value(
            db,
            tenant_id=tenant.id,
            target=FeatureTarget("channel", channel_id="19:c@thread.tacv2"),
            key="web_awareness",
            value="off",
            set_by=None,
            surface="chat",
        )
    links = workflow_cards.web_at(runtime)
    post = "19:c@thread.tacv2;messageid=1"
    assert await links(tenant.id, post, None) is None, "Off at the post's channel"
    assert await links(tenant.id, "19:other@thread.tacv2", None) is not None
    assert await workflow_cards.web_at(runtime, foreign=True)(tenant.id, "19:x", None) is None

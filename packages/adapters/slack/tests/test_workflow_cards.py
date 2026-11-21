"""Workflow cards on Slack: the confirmation reads back as shown and the run card is edited."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

from daimon.adapters.slack import workflow_cards
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.config import WebSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.workflows import chat_cards
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import mock_settings
from daimon.testing.slack_surface import SlackSurfaceHarness
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_posted_confirmation_reads_back_and_the_card_is_edited_in_place() -> None:
    harness = SlackSurfaceHarness(evidence_id="wf", session_id="s", root_turn_id="r")
    chat = workflow_cards._Chat(harness.client, "C_QA", "U_QA", "1699999999.000001")  # pyright: ignore[reportPrivateUsage]
    inputs = {"email": "jane@example.com", "note": "a & b"}
    place = await chat.post(chat_cards.confirm_card(uuid.uuid4(), "Lead", 1, inputs, None))
    posted = harness.recorder.payload(place.message_id)
    keys = frozenset(inputs)
    assert chat_cards.confirmed_inputs(workflow_cards.card_text(posted["blocks"]), keys) == inputs
    await chat.edit(place, chat_cards.starting_card("Lead"))
    edited = harness.recorder.payload(place.message_id)
    assert "Lead" in edited["text"] and "go:" not in str(edited["blocks"])


def test_the_run_form_names_its_form_and_where_the_run_card_goes() -> None:
    spec: dict[str, Any] = {
        "name": "Lead",
        "inputs": {"required": ["email"], "properties": {"email": {}}},
    }
    form = chat_cards.run_form(uuid.uuid4(), spec, limit=workflow_cards.FORM_LIMIT)
    assert form is not None
    view = workflow_cards.form_view(form, "C1", None)
    assert view["callback_id"] == workflow_cards.FORM_CALLBACK_ID
    assert [b["block_id"] for b in view["blocks"]] == ["email"]
    assert form.submit_id in view["private_metadata"]


async def test_a_workflow_card_links_the_web_only_where_web_awareness_is_on(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = mock_settings()
    settings.web = WebSettings(public_url="https://app.example.com/")  # type: ignore[arg-type]
    runtime = cast(
        SlackRuntime,
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
            target=FeatureTarget("channel", channel_id="C1"),
            key="web_awareness",
            value="off",
            set_by=None,
            surface="chat",
        )
    links = workflow_cards.web_at(runtime)
    assert await links(tenant.id, "C1", "1700.1") is None, "Off at the channel"
    assert await links(tenant.id, "C2", None) is not None, "another channel stays linked"

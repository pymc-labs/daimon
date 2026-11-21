"""Workflow cards on Discord: the confirmation reads back as shown and the follower edits."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.workflow_cards import card_editor, card_text, web_at
from daimon.core.config import WebSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.workflows import chat_cards
from daimon.core.workflows.chat_cards import ChatPlace
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import mock_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_posted_confirmation_reads_back_and_the_card_is_edited_in_place() -> None:
    harness = DiscordSurfaceHarness(evidence_id="wf", session_id="s", root_turn_id="r")
    thread = harness.thread
    inputs = {"email": "jane@example.com", "n": 3}
    card = chat_cards.confirm_card(uuid.uuid4(), "Lead", 1, inputs, None)
    sent = await thread.send(view=build_card_view(card))
    assert chat_cards.confirmed_inputs(card_text(sent.components), frozenset(inputs)) == inputs

    async def fetch(channel_id: int) -> object:
        assert channel_id == thread.id, "the card's own channel"
        return thread

    place = ChatPlace("discord", str(thread.id), str(sent.id))
    await card_editor(fetch, place, chat_cards.starting_card("Lead"))
    [shown] = harness.gateway.surviving_messages()
    assert "Lead" in shown.payload_json and "Run" not in shown.component_labels


async def test_a_workflow_card_links_the_web_only_where_web_awareness_is_on(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = mock_settings()
    settings.web = WebSettings(public_url="https://app.example.com/")  # type: ignore[arg-type]
    runtime = cast(
        DiscordRuntime,
        SimpleNamespace(
            settings=settings,
            sessionmaker=db_session_factory,
            deployment_default=DeploymentDefault(),
            anthropic=None,
            resolver_cache=new_resolver_cache(),
        ),
    )

    async def no_fetch(channel_id: int) -> object:
        raise AssertionError("a press carries its channel")

    links = web_at(runtime, no_fetch)
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        await set_feature_value(
            db,
            tenant_id=tenant.id,
            target=FeatureTarget("channel", channel_id="100"),
            key="web_awareness",
            value="off",
            set_by=None,
            surface="chat",
        )
    assert await links(tenant.id, "100", "300") is None, "Off at the channel"
    assert await links(tenant.id, "101", "301") is not None, "another channel stays linked"

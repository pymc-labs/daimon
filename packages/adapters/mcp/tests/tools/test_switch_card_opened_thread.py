"""A copy made in the first turn of a mention's reply thread switches on its card.

A Discord mention in a channel is admitted before its reply thread opens; the turn's
origin (and so the copy's `forked_in` stamp and the switch tool's capture) names that
thread. The card must capture the same place, or its digest never matches the
preview's and a copy's switch is refused as stale on every try.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.tools.agents import (
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_backend import AgentBackendRefused, make_backend_service
from daimon.core.confirmation import backend_switch_callbacks, prompt_for_tool_call
from daimon.core.stores.channel_skills import add_channel_skill
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall
from daimon.core.turn.admission import admit, capture_admitted_backend_source
from daimon.core.turn.runtimes import build_channel_runtime
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.turn_fakes import deps_for_pinned_mux

from .test_j1_switch_card_guard import _new_copy  # pyright: ignore[reportPrivateUsage]
from .test_j1_switch_card_guard import fresh_chat as fresh_chat


async def test_a_copy_cloned_in_a_mention_switches_to_gemini_on_its_card(fresh_chat):
    """Clone the default agent, preview Gemini 4 Hydrogen, confirm on the card: it applies."""
    chat = fresh_chat
    sm = chat.runtime.session_factory
    # A channel skill makes the place-bound read differ from a bare agent read.
    async with sm.begin() as db:
        await add_channel_skill(
            db,
            tenant_id=chat.auth.tenant_id,
            platform="discord",
            channel_id=chat.origin.parent_channel_id,
            skill_id="skill_01house",
            version="1",
            name="house-style",
            owner_agent_name=None,
            actor_account_id=None,
        )
    copy = await _new_copy(chat)
    deps = replace(
        deps_for_pinned_mux(chat.runtime.client, sm),
        agent_backends=True,
        deployment_default=chat.runtime.deployment_default,
        state_store=PostgresStateStore(sm),
        turn_runtimes={},
        turn_runtime_factory=build_channel_runtime,
        settings=chat.runtime.settings,
        mcp=chat.runtime.settings.mcp,
    )
    # Admitted in the channel; the reply thread opened afterwards.
    admission = await admit(
        deps,
        origin="chat",
        tenant_id=chat.auth.tenant_id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="123",
        channel_id=chat.origin.parent_channel_id,
        thread_id=None,
        now=datetime.now(UTC),
    )
    switch = partial(
        _request_agent_backend_switch_impl,
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend="gemini",
        model="gemini-4-hydrogen",
        origin_context_id=str(chat.origin.id),
    )
    preview = await switch()
    capture = partial(
        capture_admitted_backend_source,
        deps,
        admission=admission,
        tenant_id=chat.auth.tenant_id,
        account_id=chat.auth.account_id,
        platform="discord",
        opened_thread_id=chat.origin.thread_id,
        opened_thread_parent_id=chat.origin.parent_channel_id,
    )
    prepare, check = backend_switch_callbacks(
        make_backend_service(sm, chat.runtime.settings.turn, capture_source=capture),
        sessionmaker=sm,
        anthropic=chat.runtime.client,
        tenant_id=chat.auth.tenant_id,
        account_id=chat.auth.account_id,
        current_admin=AsyncMock(return_value=chat.auth.is_admin),
        current_platform_admin=AsyncMock(return_value=False),
    )
    confirm = {
        "expected_revision": preview.expected_revision,
        "preview_digest": preview.preview_digest,
        "accepted_drops": list(preview.declared_drops),
    }
    card = await prepare(
        prompt_for_tool_call(
            ToolCall(
                tool_use_id="hydrogen",
                server_name=DAIMON_SERVER_NAME,
                tool_name="request_agent_backend_switch",
                input={
                    "agent_name": copy.name,
                    "target_backend": "gemini",
                    "model": "gemini-4-hydrogen",
                    **confirm,
                },
            ),
            requester_platform_user_id="123",
            now=datetime.now(UTC),
        )
    )
    assert card.backend_switch is not None, "the card carries the switch it confirms"
    assert card.backend_switch.preview_digest == preview.preview_digest, (
        "the card shows the preview the tool returned, not a re-read of another place"
    )
    assert await check(card, "123") is None, "Confirm on the card passes its checks"
    changed = await switch(**confirm)
    assert changed.status == "changed", "the confirmed switch applies"


async def test_a_thread_opened_under_another_channel_is_not_the_cards_place(fresh_chat):
    """A thread whose parent is not the admission's channel refuses the capture."""
    chat = fresh_chat
    sm = chat.runtime.session_factory
    deps = replace(
        deps_for_pinned_mux(chat.runtime.client, sm),
        agent_backends=True,
        deployment_default=chat.runtime.deployment_default,
        state_store=PostgresStateStore(sm),
        turn_runtimes={},
        turn_runtime_factory=build_channel_runtime,
        settings=chat.runtime.settings,
        mcp=chat.runtime.settings.mcp,
    )
    admission = await admit(
        deps,
        origin="chat",
        tenant_id=chat.auth.tenant_id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="123",
        channel_id=chat.origin.parent_channel_id,
        thread_id=None,
        now=datetime.now(UTC),
    )
    copy = await _new_copy(chat)
    for parent in ("another-channel", None):
        with pytest.raises(AgentBackendRefused) as refused:
            await capture_admitted_backend_source(
                deps,
                copy,
                admission=admission,
                tenant_id=chat.auth.tenant_id,
                account_id=chat.auth.account_id,
                platform="discord",
                opened_thread_id=chat.origin.thread_id,
                opened_thread_parent_id=parent,
            )
        assert refused.value.reason == "source_context_unavailable", (
            f"a thread under {parent!r} is not this channel's reply thread"
        )

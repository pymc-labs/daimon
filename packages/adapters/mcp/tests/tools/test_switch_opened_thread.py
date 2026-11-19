"""A copy made in the first turn of a mention's reply thread switches at once.

A Discord mention in a channel is admitted before its reply thread opens; the turn's
origin, and so the copy's `forked_in` stamp, names that thread. The switch applies to
the copy at once.
"""

from __future__ import annotations

from daimon.adapters.mcp.tools.agents import (
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.stores.channel_skills import add_channel_skill

from .test_j1_switch_card_guard import _new_copy  # pyright: ignore[reportPrivateUsage]
from .test_j1_switch_card_guard import fresh_chat as fresh_chat


async def test_a_copy_cloned_in_a_mention_switches_to_gemini_at_once(fresh_chat):
    """Clone the default agent and switch it to Gemini 4 Hydrogen: it applies, no card."""
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
    changed = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend="gemini",
        model="gemini-4-hydrogen",
        origin_context_id=str(chat.origin.id),
        expected_revision=0,
    )
    assert changed.status == "changed", "the switch applies on the first call"
    assert changed.target_backend == "gemini", changed

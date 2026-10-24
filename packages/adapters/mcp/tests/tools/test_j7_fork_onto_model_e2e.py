"""J7 end to end, offline: "fork yourself onto <model>" from a member names the real
reason, the model, never an admin/owner rule (SMOKE a776e92b J7; diagnosed by dsh).

A saved copy keeps its source's model: a model that isn't available is refused as
unavailable before any permission gate, and an available different one points at a
fleet task worker. Nothing is created either way. Cheap models only.
"""

from __future__ import annotations

import pytest
from daimon.adapters.mcp.tools.agents import (
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from fastmcp.exceptions import ToolError

from packages.adapters.mcp.tests.tools.test_member_fork_switch import (
    fresh_chat as fresh_chat,
)


async def test_a_member_forking_onto_an_unavailable_model_hears_the_model_refusal(fresh_chat):
    """J7: "fork yourself onto gpt-3.5-turbo" was refused as an admin/owner rule.

    The saved-copy path must name the real obstacle, the model, before any permission
    gate, so the person is told what they can actually fix.
    """
    chat = fresh_chat
    if chat.auth.is_admin:
        pytest.skip("the member is the case under test")
    with pytest.raises(ToolError) as refused:
        await _fork_agent_impl(
            chat.runtime,
            chat.auth,
            "Daimon",
            "Daimon",
            expected_ma_agent_id="agent_daimon",
            new_copy_if_taken=True,
            model="gpt-3.5-turbo",
        )
    text = str(refused.value)
    assert "gpt-3.5-turbo" in text
    assert "isn't a known available agent model" in text, text
    assert "admin" not in text.lower() and "owner" not in text.lower(), text
    assert chat.ma.created == []


async def test_a_member_forking_onto_a_different_model_is_pointed_at_a_fleet(fresh_chat):
    """J7: a saved copy keeps its source's model; a named different model points at a fleet."""
    chat = fresh_chat
    if chat.auth.is_admin:
        pytest.skip("the member is the case under test")
    with pytest.raises(ToolError) as refused:
        await _fork_agent_impl(
            chat.runtime,
            chat.auth,
            "Daimon",
            "Daimon",
            expected_ma_agent_id="agent_daimon",
            new_copy_if_taken=True,
            model="gpt-6-luna",
        )
    text = str(refused.value)
    assert "keeps Daimon's model" in text and "fleet_spawn" in text, text
    assert "admin" not in text.lower() and "owner" not in text.lower(), text
    assert chat.ma.created == []


async def test_a_member_without_a_chat_turn_forking_onto_another_model_hears_the_model(
    fresh_chat,
):
    """#1503 review: the model check must come before EVERY permission gate. A member
    outside their own chat turn (an agent key, the hub) fails the first admin gate, and
    still hears that a saved copy keeps its source's model."""
    from dataclasses import replace

    chat = fresh_chat
    if chat.auth.is_admin:
        pytest.skip("the member is the case under test")
    outside = replace(chat.auth, chat_agent_id=None)
    with pytest.raises(ToolError) as refused:
        await _fork_agent_impl(
            chat.runtime,
            outside,
            "Daimon",
            "Daimon",
            expected_ma_agent_id="agent_daimon",
            new_copy_if_taken=True,
            model="gpt-6-luna",
        )
    text = str(refused.value)
    assert "keeps Daimon's model" in text and "fleet_spawn" in text, text
    assert "admin" not in text.lower() and "owner" not in text.lower(), text
    assert chat.ma.created == []

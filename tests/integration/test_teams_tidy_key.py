"""The key a Teams turn records its posts under is the one the tidy tools look up."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.tools.teams import _tidy as tidy
from daimon.adapters.mcp.tools.teams._send import (  # pyright: ignore[reportPrivateUsage]
    _conversation_id,
)
from daimon.adapters.teams import lifecycle as lifecycle_module
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.core.stores.agent_posts import get_post
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.factories import make_tenant
from microsoft_teams.api import MessageActivityInput, SentActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CHANNEL = "19:channel-1@thread.tacv2"


class _Sender:
    def __init__(self) -> None:
        self.count = 0

    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity:
        self.count += 1
        return SentActivity(id=activity.id or f"m-{self.count}", activity_params=activity)


@pytest.mark.parametrize("conversation", ["a:chat-1", f"{_CHANNEL};messageid=1700000000001"])
async def test_a_turns_posts_are_found_where_the_agent_names_them(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    conversation: str,
) -> None:
    async def hidden(*_: object, **__: object) -> bool:
        return False

    async def no_footer(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(lifecycle_module, "cost_footer_shown", hidden)
    monkeypatch.setattr(lifecycle_module, "balance_footer", no_footer)
    async with db_session_factory.begin() as s:
        tenant = await make_tenant(s, platform="teams", workspace_id=str(uuid.uuid4()))
    lifecycle = TeamsTurnLifecycle(
        sender=_Sender(),
        conversation_id=conversation,
        service_url="https://smba.trafficmanager.net/test",
        cancel_key="key-1",
        clock=lambda: 100.0,
        tenant_id=tenant.id,
        sessionmaker=db_session_factory,
        requester_user_id="u-1",
        ma_agent_id="ag-1",
        turn_card_intent_id=uuid.uuid4(),
    )
    await lifecycle.post_initial()
    await lifecycle.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="Hi.")]))
    answer = lifecycle.final_message_id
    assert answer is not None

    # The agent names the conversation as its context shows it: the turn's thread or chat.
    runtime = SimpleNamespace(session_factory=db_session_factory)
    ctx = SimpleNamespace(actor=SimpleNamespace(tenant_id=tenant.id))
    named = _conversation_id(conversation)
    key = await tidy._ledger_key(cast(Any, runtime), cast(Any, ctx), named, answer)  # pyright: ignore[reportPrivateUsage]
    async with db_session_factory() as s:
        post = await get_post(
            s, tenant_id=tenant.id, platform="teams", channel_id=key, message_id=answer
        )
    assert post is not None and post.role == "answer", "the answer is found and labelled"
    assert post.parent_channel_id == (None if conversation.startswith("a:") else _CHANNEL)

"""A turn's card hooks: both share the turn's group-chat roster guard."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from daimon.adapters.teams.group_chats import ChatMember, GroupChatRoster, turn_guard
from daimon.adapters.teams.provider_actions import TeamsProviderActions
from daimon.adapters.teams.tool_confirmation import TeamsConfirmationCards
from daimon.adapters.teams.turn_hooks import turn_hooks
from daimon.core.config import TurnSettings
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.tool_safety import ToolCall
from daimon.core.turn.provider_actions import ProviderActionPrompt

from .conftest import AAD_OBJECT_ID, ENTRA_TENANT_ID, GROUP_CHAT_ID, FakeSender, make_inbound
from .test_provider_actions import SESSION, prompt

OUTSIDER = ChatMember("29:x", str(uuid.UUID(int=4)), str(uuid.UUID(int=99)), "user")


def question(thread_id: str) -> ProviderActionPrompt:
    """A provider question from `thread_id` that expires unanswered at once."""
    value = prompt()
    requester = replace(value.context.requester, thread_id=thread_id)
    expires_at = datetime.now(UTC) + timedelta(milliseconds=10)
    return replace(
        value, context=replace(value.context, requester=requester, expires_at=expires_at)
    )


def hooks_for(sender: FakeSender, **kwargs: Any) -> Any:
    runtime = MagicMock()
    runtime.settings.turn = TurnSettings()
    return turn_hooks(
        TeamsConfirmationCards(sender),
        TeamsProviderActions(sender),
        runtime=runtime,
        prepared=SimpleNamespace(session_ref=SESSION),
        admission=SimpleNamespace(account_id=uuid.uuid4()),
        tenant_id=uuid.uuid4(),
        **kwargs,
    )


async def test_a_closed_group_chat_shows_neither_card_and_tells_the_asker_once() -> None:
    """Both cards check the one turn guard: the turn stops and the asker hears it once."""
    told: list[bool] = []

    async def members(_chat_id: str) -> list[ChatMember]:
        return [OUTSIDER]

    async def guests() -> frozenset[str]:
        return frozenset()

    async def tell() -> None:
        told.append(True)

    roster = GroupChatRoster(tenant_id=ENTRA_TENANT_ID, members=members, member_guests=guests)
    cancel = asyncio.Event()
    inbound = make_inbound(kind="group", conversation=GROUP_CHAT_ID)
    sender = FakeSender()
    hooks = hooks_for(
        sender,
        inbound=inbound,
        roster_open=turn_guard(roster, GROUP_CHAT_ID, cancel, tell),
    )
    call = ToolCall(tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={})
    confirm = prompt_for_tool_call(
        call, requester_platform_user_id=AAD_OBJECT_ID, now=datetime.now(UTC)
    )
    assert await hooks["confirm_write"](confirm) == "expired", "declined as on a timeout"
    assert await hooks["provider_action"](question(inbound.thread_id)) is None, "unanswered"
    assert not sender.sent, "no card shows the tool's input"
    assert cancel.is_set(), "the turn stops"
    assert told == [True], "told once per turn, not once per card"

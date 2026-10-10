"""Teams delivery of the channel budget notice."""

from __future__ import annotations

import uuid
from dataclasses import MISSING, fields
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
from daimon.adapters.teams.budget_notice import teams_budget_notifier, with_budget_notifier
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.channel_budget_notice import BudgetNotice
from daimon.core.turn.deps import TurnDeps

_TENANT = uuid.uuid4()


def _required(cls: Any, **given: object) -> dict[str, object]:
    needed = [f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING]
    return {name: MagicMock() for name in needed} | given


async def test_recipients_on_the_roster_get_a_1_1_message() -> None:
    settings = MagicMock()
    settings.direct_message_policies = {}
    runtime = TeamsRuntime(**_required(TeamsRuntime, settings=settings))
    roster = {"aad-1": "29:one", "aad-3": "29:three"}

    async def member(conversation_id: str, aad: str) -> str | None:
        assert conversation_id == "19:chan@thread.tacv2"
        if aad == "aad-3":
            raise httpx.ConnectError("down")
        return roster.get(aad)

    direct = MagicMock()
    direct.member = AsyncMock(side_effect=member)
    direct.open_chat = AsyncMock(side_effect=lambda m: f"chat-{m}")
    direct.post = AsyncMock()
    notice = BudgetNotice(
        tenant_id=_TENANT,
        workspace_id="entra",
        platform="teams",
        channel_id="19:chan@thread.tacv2",
        recipient_ids=("aad-1", "aad-2", "aad-3"),
        limit_line="$1.00",
        monthly=True,
        budget_id=uuid.uuid4(),
        window_key="window",
    )
    delivered = await teams_budget_notifier(runtime, direct)(notice)
    assert delivered == 1, "only a DM that landed counts"

    direct.post.assert_awaited_once()
    chat, text = direct.post.await_args.args
    assert chat == "chat-29:one"
    assert text == (
        "Channel `19:chan@thread.tacv2` has used its $1.00 monthly budget.\n\n"
        "Raise the budget to resume now, or wait until next month."
    ), text


def test_a_runtime_without_1_1_chats_gets_no_notifier() -> None:
    runtime = TeamsRuntime(**_required(TeamsRuntime, turn_deps=TurnDeps(**_required(TurnDeps))))
    assert with_budget_notifier(runtime, None) is runtime
    assert with_budget_notifier(runtime, MagicMock()).turn_deps.budget_notifier is not None

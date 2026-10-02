"""Slack delivery of the channel budget notice."""

from __future__ import annotations

import uuid
from dataclasses import MISSING, fields
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack import budget_notice
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.channel_budget_notice import BudgetNotice
from daimon.core.config import DirectMessagePolicy
from daimon.core.turn.deps import TurnDeps
from slack_sdk.errors import SlackApiError

_TENANT = uuid.uuid4()


def _required(cls: Any, **given: object) -> dict[str, object]:
    needed = [f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING]
    return {name: MagicMock() for name in needed} | given


def _notice() -> BudgetNotice:
    return BudgetNotice(
        tenant_id=_TENANT,
        workspace_id="T1",
        platform="slack",
        channel_id="C1",
        recipient_ids=("U1", "U2", "U3"),
        budget_line="$2.00 of $2.00 (total)",
        monthly=False,
    )


async def test_each_allowed_recipient_gets_a_dm_and_one_failure_skips_only_them() -> None:
    settings = MagicMock()
    settings.direct_message_policies = {
        _TENANT: DirectMessagePolicy(mode="allowlist", recipient_ids=["U1", "U2"])
    }
    runtime = SlackRuntime(**_required(SlackRuntime, settings=settings))
    client = MagicMock()

    async def opened(*, users: str) -> dict[str, Any]:
        if users == "U1":
            raise SlackApiError("closed", MagicMock())
        return {"channel": {"id": f"D-{users}"}}

    client.conversations_open = AsyncMock(side_effect=opened)
    client.chat_postMessage = AsyncMock()
    resolve = AsyncMock(return_value=client)
    with patch.object(budget_notice, "resolve_web_client", resolve):
        await budget_notice.slack_budget_notifier(runtime)(_notice())

    resolve.assert_awaited_once_with(runtime, team_id="T1")
    assert [c.kwargs["users"] for c in client.conversations_open.await_args_list] == ["U1", "U2"]
    client.chat_postMessage.assert_awaited_once()
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == "D-U2"
    assert kwargs["text"] == (
        "<#C1>'s budget is used up: $2.00 of $2.00 (total). "
        "New turns there are refused until an admin raises it."
    )


def test_a_real_runtime_gets_the_notifier() -> None:
    runtime = SlackRuntime(**_required(SlackRuntime, turn_deps=TurnDeps(**_required(TurnDeps))))
    assert budget_notice.with_budget_notifier(runtime).turn_deps.budget_notifier is not None
    stand_in = MagicMock()
    assert budget_notice.with_budget_notifier(stand_in) is stand_in

"""Delivers the channel budget notice (`daimon.core.channel_budget_notice`) by DM."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import structlog
from daimon.adapters.slack.channel_admin_groups import stored_group_members
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.channel_budget_notice import BudgetNotice, BudgetNotifier
from daimon.core.config import DirectMessagePolicy
from daimon.core.turn.deps import TurnDeps
from slack_sdk.errors import SlackApiError

log = structlog.get_logger()


def slack_budget_notifier(runtime: SlackRuntime) -> BudgetNotifier:
    """DM each recipient the DM policy allows; a failed DM skips that one."""

    async def send(notice: BudgetNotice) -> None:
        client = await resolve_web_client(runtime, team_id=notice.workspace_id)
        if client is None:
            return
        text = notice.text(f"<#{notice.channel_id}>")
        policy = runtime.settings.direct_message_policies.get(
            notice.tenant_id, DirectMessagePolicy()
        )
        for user_id in notice.allowed_recipients(policy):
            try:
                opened = await client.conversations_open(users=user_id)  # pyright: ignore[reportUnknownMemberType]
                channel = cast("dict[str, str]", opened["channel"])["id"]
                await client.chat_postMessage(channel=channel, text=text)  # pyright: ignore[reportUnknownMemberType]
            except SlackApiError as exc:
                log.info("channel_budget.notice_undelivered", error=str(exc))

    return send


def with_budget_notifier(runtime: SlackRuntime) -> SlackRuntime:
    """`runtime` whose turns send the notice; a stand-in test runtime comes back unchanged."""
    real = cast(object, runtime)
    deps = cast(object, runtime.turn_deps)
    if not isinstance(real, SlackRuntime) or not isinstance(deps, TurnDeps):
        return runtime
    notifier = slack_budget_notifier(real)
    return replace(
        real,
        turn_deps=replace(deps, budget_notifier=notifier, group_members=stored_group_members(real)),
    )

"""Delivers the channel budget notice (`daimon.core.channel_budget_notice`) by DM,
and an alert-only budget's alert in its channel."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import structlog
from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.user_groups import user_group_lookup
from daimon.core.channel_budget_notice import (
    BudgetAlert,
    BudgetAlerter,
    BudgetNotice,
    BudgetNotifier,
)
from daimon.core.config import env_dm_policy
from daimon.core.dm_access import sender_admits
from daimon.core.turn.deps import TurnDeps
from slack_sdk.errors import SlackApiError

log = structlog.get_logger()


def slack_budget_notifier(runtime: SlackRuntime) -> BudgetNotifier:
    """DM each recipient the DM policy and the default agent's grant allow; skip failed DMs."""

    async def send(notice: BudgetNotice) -> int:
        client = await resolve_web_client(runtime, team_id=notice.workspace_id)
        if client is None:
            return 0
        text = notice.text(f"<#{notice.channel_id}>")
        policy = env_dm_policy(runtime.settings.direct_message_policies, notice.tenant_id)
        groups = user_group_lookup(runtime, client, team_id=notice.workspace_id)

        async def admits(user_id: str) -> bool:
            return await sender_admits(
                runtime.sessionmaker,
                tenant_id=notice.tenant_id,
                agent_name=None,
                user_id=user_id,
                groups=groups,
            )

        delivered = 0
        for user_id in await notice.allowed_recipients(policy, admits):
            try:
                opened = await client.conversations_open(users=user_id)  # pyright: ignore[reportUnknownMemberType]
                channel = cast("dict[str, str]", opened["channel"])["id"]
                await client.chat_postMessage(channel=channel, **notice_fields(text))  # pyright: ignore[reportUnknownMemberType]
                delivered += 1
            except SlackApiError as exc:
                log.info("channel_budget.notice_undelivered", error=str(exc))
        return delivered

    return send


def slack_budget_alerter(runtime: SlackRuntime) -> BudgetAlerter:
    """Post the alert into its channel; False when Slack refuses (a channel Daimon is not in)."""

    async def send(alert: BudgetAlert) -> bool:
        if alert.slack_team_id is None:
            return False
        client = await resolve_web_client(runtime, team_id=alert.slack_team_id)
        if client is None:
            return False
        try:
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=alert.destination_id, **notice_fields(alert.text(f"<#{alert.channel_id}>"))
            )
        except SlackApiError as exc:
            log.info("channel_budget.alert_undelivered", error=str(exc))
            return False
        return True

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
        turn_deps=replace(
            deps,
            budget_notifier=notifier,
            budget_alerter=slack_budget_alerter(real),
        ),
    )

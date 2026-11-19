"""Delivers the channel budget notice (`daimon.core.channel_budget_notice`) by DM,
and an alert-only budget's alert in its channel."""

from __future__ import annotations

from dataclasses import replace
from typing import Final, cast

import structlog
from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.user_groups import user_group_lookup
from daimon.core.channel_budget_notice import (
    AlertOutcome,
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


_REFUSED_ERRORS: Final = frozenset(
    {"channel_not_found", "not_in_channel", "is_archived", "access_denied", "restricted_action"}
)
"""Slack's answers that the next post would get too: the alert waits for the next multiple."""


def slack_budget_alerter(runtime: SlackRuntime) -> BudgetAlerter:
    """Post the alert as a neutral card; `refused` for a channel Daimon cannot post in."""

    async def send(alert: BudgetAlert) -> AlertOutcome:
        if alert.slack_team_id is None:
            return "refused"
        client = await resolve_web_client(runtime, team_id=alert.slack_team_id)
        if client is None:
            return "refused"  # no stored bot token for the destination's workspace
        ref = alert.channel_ref(f"<#{alert.channel_id}>", await _team_name(runtime, alert))
        try:
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=alert.destination_id, **notice_fields(alert.text(ref), neutral=True)
            )
        except SlackApiError as exc:
            raw = cast(object, exc.response.get("error"))  # pyright: ignore[reportUnknownMemberType]
            error = raw if isinstance(raw, str) else None
            log.info("channel_budget.alert_undelivered", error=str(error))
            return "refused" if error in _REFUSED_ERRORS else "failed"
        return "sent"

    return send


async def _team_name(runtime: SlackRuntime, alert: BudgetAlert) -> str | None:
    """The budget's own workspace name, for an alert posted in another workspace."""
    if alert.own:
        return None
    client = await resolve_web_client(runtime, team_id=alert.workspace_id)
    if client is None:
        return None
    try:
        info = await client.team_info()  # pyright: ignore[reportUnknownMemberType]
    except SlackApiError:
        return None
    team = info.get("team")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    name = cast("dict[str, object]", team).get("name") if isinstance(team, dict) else None
    return name if isinstance(name, str) else None


def with_budget_notifier(runtime: SlackRuntime) -> SlackRuntime:
    """`runtime` whose turns send the notice; a stand-in test runtime comes back unchanged."""
    real = cast(object, runtime)
    deps = cast(object, runtime.turn_deps)
    if not isinstance(real, SlackRuntime) or not isinstance(deps, TurnDeps):
        return runtime
    return replace(real, turn_deps=replace(deps, budget_notifier=slack_budget_notifier(real)))

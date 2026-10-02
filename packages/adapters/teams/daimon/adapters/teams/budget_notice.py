"""Delivers the channel budget notice (`daimon.core.channel_budget_notice`) in 1:1 chats."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import structlog
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.channel_budget_notice import BudgetNotice, BudgetNotifier
from daimon.core.config import DirectMessagePolicy
from daimon.core.turn.deps import TurnDeps

log = structlog.get_logger()


def teams_budget_notifier(runtime: TeamsRuntime, direct: DirectChats) -> BudgetNotifier:
    """Message each recipient the DM policy allows who is on the channel's team roster."""

    async def send(notice: BudgetNotice) -> int:
        policies = runtime.settings.direct_message_policies
        text = notice.text(f"Channel `{notice.channel_id}`")
        delivered = 0
        for user_id in notice.allowed_recipients(
            policies.get(notice.tenant_id, DirectMessagePolicy())
        ):
            try:
                member = await direct.member(notice.channel_id, user_id)
                if member is not None:
                    await direct.post(await direct.open_chat(member), text)
                    delivered += 1
            except TEAMS_SEND_ERRORS as exc:
                log.info("channel_budget.notice_undelivered", err_type=type(exc).__name__)
        return delivered

    return send


def with_budget_notifier(runtime: TeamsRuntime, direct: DirectChats | None) -> TeamsRuntime:
    """`runtime` whose turns send the notice; unchanged without 1:1 chats or for a stand-in."""
    real = cast(object, runtime)
    deps = cast(object, runtime.turn_deps)
    if direct is None or not isinstance(real, TeamsRuntime) or not isinstance(deps, TurnDeps):
        return runtime
    notifier = teams_budget_notifier(real, direct)
    return replace(real, turn_deps=replace(deps, budget_notifier=notifier))

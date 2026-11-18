"""Slack cards drop their web button where `web_awareness` is Off."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.web_button import spend_url, web_links_at
from daimon.core.config import WebSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import mock_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CHANNEL = "C1"
TARGETS = [
    FeatureTarget("workspace"),
    FeatureTarget("channel", channel_id=CHANNEL),
    FeatureTarget("agent", agent_id="agent_a"),
]


def _runtime(sessions: async_sessionmaker[AsyncSession]) -> SlackRuntime:
    settings = mock_settings()
    settings.web = WebSettings(public_url="https://app.example.com/")  # type: ignore[arg-type]
    return cast(
        SlackRuntime,
        SimpleNamespace(
            settings=settings,
            sessionmaker=sessions,
            deployment_default=DeploymentDefault(),
            anthropic=None,
            resolver_cache=new_resolver_cache(),
        ),
    )


@pytest.mark.parametrize("target", [None, *TARGETS], ids=["on", "workspace", "channel", "agent"])
async def test_slack_cards_drop_the_web_button_when_web_awareness_is_off(
    db_session_factory: async_sessionmaker[AsyncSession], target: FeatureTarget | None
) -> None:
    team = "T1"
    async with db_session_factory.begin() as db:
        await make_tenant(db, platform="slack", workspace_id=team)
        if target is not None:
            tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team)
            await set_feature_value(
                db,
                tenant_id=tenant_id,
                target=target,
                key="web_awareness",
                value="off",
                set_by=None,
                surface="chat",
            )
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team)
    runtime = _runtime(db_session_factory)
    spend = await spend_url(runtime, tenant_id, CHANNEL)
    agent = await web_links_at(runtime, tenant_id, admin=True, channel_id="C2", agent_id="agent_a")
    assert (spend is None) is (target is not None and target.tier != "agent"), (
        "spend and channel cards follow the workspace and channel values"
    )
    assert (agent is None) is (target is not None and target.tier in ("workspace", "agent")), (
        "an agent card follows the workspace and agent values, not another channel's"
    )

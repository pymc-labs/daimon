"""Discord cards drop their web button where `web_awareness` is Off."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import discord
import pytest
from daimon.adapters.discord.billing_panel.panel import BotInteraction, web_spend_url
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import WebSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import mock_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD = "745"


class _Thread(discord.Thread):
    """Thread "20" under channel "10"."""

    def __init__(self) -> None:
        self.id, self.parent_id = 20, 10


@pytest.mark.parametrize(
    ("target", "channel", "linked"),
    [
        (None, "10", True),
        (FeatureTarget("workspace"), "10", False),
        (FeatureTarget("channel", channel_id="10"), "10", False),
        (FeatureTarget("channel", channel_id="10"), "11", True),
        (FeatureTarget("thread", channel_id="10", thread_key="20"), "20", False),
    ],
    ids=["on", "workspace-off", "channel-off", "other-channel", "thread-off"],
)
async def test_billing_panel_drops_the_web_button_when_web_awareness_is_off(
    db_session_factory: async_sessionmaker[AsyncSession],
    target: FeatureTarget | None,
    channel: str,
    linked: bool,
) -> None:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=GUILD)
    async with db_session_factory.begin() as db:
        await make_tenant(db, platform="discord", workspace_id=GUILD)
        if target is not None:
            await set_feature_value(
                db,
                tenant_id=tenant_id,
                target=target,
                key="web_awareness",
                value="off",
                set_by=None,
                surface="chat",
            )
    settings = mock_settings()
    settings.web = WebSettings(public_url="https://app.example.com/")  # type: ignore[arg-type]
    runtime = cast(
        DiscordRuntime,
        SimpleNamespace(
            settings=settings,
            sessionmaker=db_session_factory,
            deployment_default=DeploymentDefault(),
            anthropic=None,
            resolver_cache=new_resolver_cache(),
        ),
    )
    # Channel "20" is a thread under "10".
    thread = _Thread()
    where = thread if channel == "20" else SimpleNamespace(id=int(channel))
    interaction = cast(BotInteraction, SimpleNamespace(channel=where, channel_id=int(channel)))
    url = await web_spend_url(runtime, GUILD, is_admin=False, interaction=interaction)
    assert (url is not None) is linked, "the spend link follows web_awareness at the channel"

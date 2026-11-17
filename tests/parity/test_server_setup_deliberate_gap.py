"""Executable record of where a worker's settings panel and its sessions read setup apart.

A worker need not carry setup only the MCP server uses (`SERVER_SETUP`, such as the notebook
host), so it can't tell that setup is missing from that only the MCP server has it. Its
sessions keep those tools for the MCP server to judge, while its panels read the feature as
Not set up: they offer Off, its lock and Default, which still decide what a session gets, and
refuse On, which they can't show to be in effect.
"""

from __future__ import annotations

import uuid

from daimon.core import settings_panel as panel
from daimon.core.admin_actions import DelegatedAction
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features.registry import DEFAULT, FEATURES
from daimon.core.features.service import FeatureActor, feature_refusal
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.surface import parse_choice
from daimon.core.features.toolset import place_toolset
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

WORKER = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        "discord": DiscordSettings(bot_token=SecretStr("test")),
    }
)
"""A worker without the notebook host settings, which only the MCP server carries."""


def _actor(tenant_id: uuid.UUID) -> FeatureActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return True

    return FeatureActor(tenant_id, None, "chat", "person", admin_for)


async def test_a_worker_panel_reads_not_set_up_where_its_sessions_keep_the_tools(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The session keeps the notebook tools; the panel reads Not set up and offers only what a
    session still obeys: Off, its lock and Default."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    place = FeaturePlace.of(tenant.id, "discord", "C1")
    spec = FEATURES["notebooks"]
    toolset = await place_toolset(db_session_factory, WORKER, place, ma_agent(tenant_id=tenant.id))
    assert "notebooks" not in toolset.off_capabilities, "the session kept the notebook tools"
    op = panel.open_op(spec.category, spec.key)
    screen = await panel.show(db_session_factory, WORKER, _actor(tenant.id), place, op)
    assert screen is not None and "Not set up" in screen.lines[0], "the panel reads Not set up"
    offered = {
        (picked.value, picked.locked)
        for select in screen.selects
        for option in select.options
        if (picked := parse_choice(option.value)) is not None
    }
    assert offered and {value for value, _ in offered} <= {spec.off, DEFAULT}, offered
    assert (spec.off, True) in offered, "the panel offers the Off lock"
    for value, refused in ((spec.off, False), (DEFAULT, False), ("on", True)):
        refusal = feature_refusal(
            "notebooks", "workspace", value, locked=False, place=place, settings=WORKER
        )
        assert (refusal is not None) is refused, f"{value} refused: {refusal}"
        assert refusal is None or refusal.reason == "deployment_disabled", refusal

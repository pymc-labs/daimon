"""/memory command — listing and content display, resolution wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.discord.commands.memory import MemoryCog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)

pytestmark = pytest.mark.asyncio

GUILD_ID = 123456789


def _interaction(runtime: DiscordRuntime) -> MagicMock:
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.guild_id = GUILD_ID
    interaction.channel_id = 42
    interaction.user.id = 777
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def _setup(db_session, db_session_factory, *, seed: dict[str, str]):
    """Create tenant + MA agent + channel-resolvable config + bound store."""
    # make_tenant derives tenant_id from (platform, workspace_id) itself — it
    # doesn't take an id= override — so passing the same workspace_id the
    # command derives from (str(GUILD_ID)) with the default platform="discord"
    # reproduces the exact tenant_id the command will compute.
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    tenant = await make_tenant(db_session, workspace_id=str(GUILD_ID))
    assert tenant.id == tenant_id
    mem_state = FakeMemoryStoreState()
    client = build_fake_anthropic(
        combine_handlers(make_fake_memory_store_handler(mem_state), make_fake_ma_handler())
    )
    agent = await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
    store = await client.beta.memory_stores.create(name="m", description="d")
    for path, content in seed.items():
        await client.beta.memory_stores.memories.create(store.id, path=path, content=content)
    await insert_memory_store(
        db_session, tenant_id=tenant_id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()

    runtime = MagicMock(spec=DiscordRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory
    # deployment_default supplies agent_name when no channel/tenant override exists
    from daimon.core.scope import DeploymentDefault

    runtime.deployment_default = DeploymentDefault(agent_name="daimon", environment_name="default")
    return runtime


async def test_memory_list_shows_paths(db_session, db_session_factory) -> None:
    runtime = await _setup(
        db_session,
        db_session_factory,
        seed={"/notes/a.md": "alpha", "/prefs/style.md": "tabs"},
    )
    cog = MemoryCog(MagicMock())
    interaction = _interaction(runtime)

    await cog.memory.callback(cog, interaction, path=None)

    sent = interaction.followup.send.call_args
    text = sent.args[0] if sent.args else sent.kwargs.get("content", "")
    assert "/notes/a.md" in text and "/prefs/style.md" in text
    assert sent.kwargs.get("ephemeral") is True


async def test_memory_show_renders_content(db_session, db_session_factory) -> None:
    runtime = await _setup(db_session, db_session_factory, seed={"/notes/a.md": "alpha"})
    cog = MemoryCog(MagicMock())
    interaction = _interaction(runtime)

    await cog.memory.callback(cog, interaction, path="/notes/a.md")

    sent = interaction.followup.send.call_args
    text = sent.args[0] if sent.args else sent.kwargs.get("content", "")
    assert "alpha" in text


async def test_memory_show_truncates_content_with_closed_fence(
    db_session, db_session_factory
) -> None:
    """Boundary case: content ~2x the platform limit must still render under
    Discord's hard 2000-char cap with a CLOSED code fence — never truncated
    mid-fence, which would corrupt rendering for the rest of the message."""
    huge_content = "x" * 3800  # ~2x _DISCORD_LIMIT (1900)
    runtime = await _setup(
        db_session, db_session_factory, seed={"/notes/big.md": huge_content}
    )
    cog = MemoryCog(MagicMock())
    interaction = _interaction(runtime)

    await cog.memory.callback(cog, interaction, path="/notes/big.md")

    sent = interaction.followup.send.call_args
    text = sent.args[0] if sent.args else sent.kwargs.get("content", "")
    assert len(text) <= 2000, f"exceeds Discord's hard cap: {len(text)} chars"
    assert text.rstrip().endswith("```"), f"fence not closed: {text[-40:]!r}"
    assert "truncated" in text


async def test_memory_empty_state(db_session, db_session_factory) -> None:
    runtime = await _setup(db_session, db_session_factory, seed={})
    cog = MemoryCog(MagicMock())
    interaction = _interaction(runtime)

    await cog.memory.callback(cog, interaction, path=None)

    sent = interaction.followup.send.call_args
    text = sent.args[0] if sent.args else sent.kwargs.get("content", "")
    assert "no memories" in text.lower()

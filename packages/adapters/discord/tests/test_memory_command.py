"""/memory command — listing and content display, resolution wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from daimon.adapters.discord.commands.memory import MemoryCog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)

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


async def test_memory_show_escapes_embedded_fences(db_session, db_session_factory) -> None:
    """Content containing ``` (agents store markdown with code blocks) must
    not break out of the wrapping fence: exactly one opening and one closing
    fence in the final message."""
    content = "notes\n```python\nprint('hi')\n```\nmore notes"
    runtime = await _setup(db_session, db_session_factory, seed={"/notes/code.md": content})
    cog = MemoryCog(MagicMock())
    interaction = _interaction(runtime)

    await cog.memory.callback(cog, interaction, path="/notes/code.md")

    sent = interaction.followup.send.call_args
    text = sent.args[0] if sent.args else sent.kwargs.get("content", "")
    assert text.count("```") == 2, f"embedded fence broke out: {text!r}"
    assert "print('hi')" in text


async def test_memory_show_truncates_content_with_closed_fence(
    db_session, db_session_factory
) -> None:
    """Boundary case: content ~2x the platform limit must still render under
    Discord's hard 2000-char cap with a CLOSED code fence — never truncated
    mid-fence, which would corrupt rendering for the rest of the message."""
    huge_content = "x" * 3800  # ~2x _DISCORD_LIMIT (1900)
    runtime = await _setup(db_session, db_session_factory, seed={"/notes/big.md": huge_content})
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
    assert "no memory to show here." in text.lower()


async def test_memory_hides_an_agent_isolation_keeps_out(db_session, db_session_factory) -> None:
    """In an isolated channel only its own agents are visible, so a shared agent's memory isn't."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy

    runtime = await _setup(db_session, db_session_factory, seed={"/notes/a.md": "alpha"})
    await set_access_policy(
        db_session,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID)),
        policy=TenantAccessPolicy(sealed_channel_ids=("42",), isolated_channel_ids=("42",)),
    )
    await db_session.commit()
    cog = MemoryCog(MagicMock())
    interaction = _interaction(runtime)

    await cog.memory.callback(cog, interaction, path=None)

    text = interaction.followup.send.call_args.args[0]
    assert "no memory to show here." in text.lower(), "the deployment default answers elsewhere too"


async def _seed_personal(db_session, runtime, *, user_id: int, files: dict[str, str]) -> str:
    """Bind a personal store for the member `user_id`, as their first chat session would."""
    from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
    from daimon.core.stores.agent_memory_stores import insert_personal_memory_store
    from daimon.core.stores.identity import get_or_create_platform_principal

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name="daimon")
    assert agent is not None
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant_id, platform="discord", external_id=str(user_id)
    )
    store = await runtime.anthropic.beta.memory_stores.create(name=f"personal {user_id}")
    for path, content in files.items():
        await runtime.anthropic.beta.memory_stores.memories.create(
            store.id, path=path, content=content
        )
    await insert_personal_memory_store(
        db_session,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        account_id=principal.account_id,
        memory_store_id=store.id,
    )
    await db_session.commit()
    return store.id


async def _paths(runtime, store_id: str) -> list[str]:
    page = await runtime.anthropic.beta.memory_stores.memories.list(store_id, path_prefix="/")
    return sorted([item.path async for item in page])


async def _say(runtime, *, user_id: int, path: str | None) -> str:
    cog = MemoryCog(MagicMock())
    interaction = _interaction(runtime)
    interaction.user.id = user_id
    await cog.memory.callback(cog, interaction, path=path)
    sent = interaction.followup.send.call_args
    assert sent.kwargs.get("ephemeral") is True
    return sent.args[0]


async def test_memory_mine_shows_only_the_invokers_own_personal_memory(
    db_session, db_session_factory
) -> None:
    runtime = await _setup(db_session, db_session_factory, seed={"/notes/team.md": "fridays"})
    await _seed_personal(db_session, runtime, user_id=777, files={"/style.md": "alice-tabs"})
    await _seed_personal(db_session, runtime, user_id=888, files={"/tone.md": "bob-terse"})

    alice = await _say(runtime, user_id=777, path="mine")
    alice_file = await _say(runtime, user_id=777, path="mine /style.md")
    bob_probe = await _say(runtime, user_id=888, path="mine /style.md")

    assert "/style.md" in alice and "/tone.md" not in alice
    assert "alice-tabs" in alice_file
    assert "alice-tabs" not in bob_probe, "a path never reaches another member's store"
    assert "No file called /style.md" in bob_probe


async def test_forget_mine_needs_confirmation_and_never_clears_another_members(
    db_session, db_session_factory
) -> None:
    import re

    runtime = await _setup(db_session, db_session_factory, seed={"/notes/team.md": "fridays"})
    alice_store = await _seed_personal(db_session, runtime, user_id=777, files={"/style.md": "x"})
    bob_store = await _seed_personal(db_session, runtime, user_id=888, files={"/tone.md": "y"})
    asked = await _say(runtime, user_id=777, path="forget-mine")
    found = re.search(r"forget-mine ([0-9a-f]{6})", asked)
    assert found is not None, "the preview offers the code"
    alice_code = found.group(1)
    assert await _paths(runtime, alice_store) == ["/style.md"], (
        "the first step only shows what would go, with its code"
    )

    bob_with_alices_code = await _say(runtime, user_id=888, path=f"forget-mine {alice_code}")
    assert "Nothing was deleted" in bob_with_alices_code
    assert await _paths(runtime, alice_store) == ["/style.md"]
    assert await _paths(runtime, bob_store) == ["/tone.md"]

    done = await _say(runtime, user_id=777, path=f"forget-mine {alice_code}")
    assert "Deleted 1 file " in done
    assert await _paths(runtime, alice_store) == []
    assert await _paths(runtime, bob_store) == ["/tone.md"], "only the invoker's store"
    shared = await _say(runtime, user_id=777, path=None)
    assert "/notes/team.md" in shared, "the shared memory is untouched"


async def _hold_channel(db_session, runtime, *, shared_files: dict[str, str]) -> None:
    """Mark the invoking channel shared and bind a channel store holding `shared_files`."""
    from daimon.core.stores.agent_memory_stores import insert_channel_memory_store
    from daimon.core.stores.shared_channels import mark_channel_shared

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    (agent,) = [a async for a in runtime.anthropic.beta.agents.list() if a.name == "daimon"]
    store = await runtime.anthropic.beta.memory_stores.create(name="channel", description="d")
    for path, content in shared_files.items():
        await runtime.anthropic.beta.memory_stores.memories.create(
            store.id, path=path, content=content
        )
    await insert_channel_memory_store(
        db_session,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        platform="discord",
        channel_id="42",
        memory_store_id=store.id,
    )
    await mark_channel_shared(
        db_session, tenant_id=tenant_id, platform="discord", channel_id="42", source="platform"
    )
    await db_session.commit()


def _sent_text(interaction: MagicMock) -> str:
    sent = interaction.followup.send.call_args
    return sent.args[0] if sent.args else sent.kwargs.get("content", "")


async def test_memory_in_a_shared_channel_shows_the_channel_store_not_the_agent_wide_one(
    db_session, db_session_factory
) -> None:
    runtime = await _setup(db_session, db_session_factory, seed={"/wide.md": "internal"})
    await _hold_channel(db_session, runtime, shared_files={"/channel.md": "agreed here"})
    cog = MemoryCog(MagicMock())

    listing, shown, wide = (_interaction(runtime) for _ in range(3))
    for interaction, path in ((listing, None), (shown, "/channel.md"), (wide, "/wide.md")):
        await cog.memory.callback(cog, interaction, path=path)

    assert "/channel.md" in _sent_text(listing) and "/wide.md" not in _sent_text(listing)
    assert "agreed here" in _sent_text(shown)
    assert "No memory file called" in _sent_text(wide)


async def test_personal_memory_commands_say_it_is_not_used_in_a_shared_channel(
    db_session, db_session_factory
) -> None:
    runtime = await _setup(db_session, db_session_factory, seed={})
    await _hold_channel(db_session, runtime, shared_files={"/channel.md": "agreed here"})
    cog = MemoryCog(MagicMock())

    for path in ("mine", "mine /a.md", "forget-mine", "forget-mine abc123"):
        interaction = _interaction(runtime)
        await cog.memory.callback(cog, interaction, path=path)
        assert "Personal memory isn't used in this channel." in _sent_text(interaction)

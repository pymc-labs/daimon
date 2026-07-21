"""CLI memory subcommand — list and show."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import typer
from daimon.adapters.cli.commands.memory import memory_list_impl, memory_show_impl
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
from rich.console import Console

pytestmark = pytest.mark.asyncio


async def _setup(db_session, db_session_factory, *, content: str = "alpha"):
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id="999")
    tenant = await make_tenant(db_session, workspace_id="999")
    assert tenant.id == tenant_id
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(
        combine_handlers(make_fake_memory_store_handler(state), make_fake_ma_handler())
    )
    agent = await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
    store = await client.beta.memory_stores.create(name="m", description="d")
    await client.beta.memory_stores.memories.create(store.id, path="/a.md", content=content)
    await insert_memory_store(
        db_session, tenant_id=tenant_id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()
    rt = MagicMock()
    rt.anthropic = client
    rt.sessionmaker = db_session_factory
    return rt


async def test_memory_list_prints_paths(db_session, db_session_factory) -> None:
    rt = await _setup(db_session, db_session_factory)
    console = Console(record=True)
    await memory_list_impl(
        rt=rt,
        console=console,
        platform="discord",
        workspace="999",
        agent="daimon",
        as_json=False,
    )
    assert "/a.md" in console.export_text()


async def test_memory_show_prints_content(db_session, db_session_factory) -> None:
    rt = await _setup(db_session, db_session_factory)
    console = Console(record=True)
    await memory_show_impl(
        rt=rt,
        console=console,
        path="/a.md",
        platform="discord",
        workspace="999",
        agent="daimon",
    )
    assert "alpha" in console.export_text()


async def test_memory_show_prints_markup_like_content_verbatim(
    db_session, db_session_factory
) -> None:
    """Memory content is untrusted text: Rich markup-shaped tokens must print
    literally rather than raise MarkupError or restyle the output."""
    rt = await _setup(db_session, db_session_factory, content="[red]styled[/red] [/bold]")
    console = Console(record=True)
    await memory_show_impl(
        rt=rt,
        console=console,
        path="/a.md",
        platform="discord",
        workspace="999",
        agent="daimon",
    )
    assert "[red]styled[/red] [/bold]" in console.export_text()


async def test_memory_list_rejects_invalid_platform(db_session, db_session_factory) -> None:
    rt = await _setup(db_session, db_session_factory)
    console = Console(record=True)
    with pytest.raises(typer.BadParameter, match="unsupported platform"):
        await memory_list_impl(
            rt=rt,
            console=console,
            platform="dscord",
            workspace="999",
            agent="daimon",
            as_json=False,
        )


async def test_memory_show_rejects_invalid_platform(db_session, db_session_factory) -> None:
    rt = await _setup(db_session, db_session_factory)
    console = Console(record=True)
    with pytest.raises(typer.BadParameter, match="unsupported platform"):
        await memory_show_impl(
            rt=rt,
            console=console,
            path="/a.md",
            platform="dscord",
            workspace="999",
            agent="daimon",
        )

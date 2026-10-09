"""Rollback cleanup for Discord agent roles."""

from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

import httpx
import pytest
import typer
from daimon.adapters.cli.commands.agent_roles import roles_purge
from daimon.core.errors import StoreError
from daimon.core.stores.discord_agent_roles import save_role
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


def _settings() -> SimpleNamespace:
    return SimpleNamespace(discord=SimpleNamespace(bot_token=SecretStr("test-token")))


async def _seed(session: AsyncSession, *, guild_id: str, role_id: str) -> None:
    tenant = await make_tenant(session, platform="discord", workspace_id=guild_id)
    await save_role(
        session,
        tenant_id=tenant.id,
        ma_agent_id=f"agent-{role_id}",
        role_id=role_id,
        agent_name=f"Agent {role_id}",
    )
    await session.commit()


async def _run(
    factory: async_sessionmaker[AsyncSession],
    *,
    guild_id: str | None = None,
    all_workspaces: bool = False,
    apply: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, str]]:
    output = StringIO()
    await roles_purge(
        rt=build_cli_runtime(factory, settings=_settings()),
        console=Console(file=output, force_terminal=False),
        platform=None if all_workspaces else "discord",
        workspace_id=guild_id,
        all_workspaces=all_workspaces,
        apply=apply,
        as_json=True,
        transport=transport,
    )
    return json.loads(output.getvalue())


@pytest.mark.asyncio
async def test_purge_previews_then_deletes_scoped_roles_idempotently(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(db_session, guild_id="123", role_id="456")
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.method == "DELETE"
        assert request.headers["Authorization"] == "Bot test-token"
        return httpx.Response(204)

    transport = httpx.MockTransport(respond)
    assert await _run(db_session_factory, guild_id="123", transport=transport) == [
        {
            "workspace_id": "123",
            "agent_name": "Agent 456",
            "role_id": "456",
            "status": "would_delete",
        }
    ]
    assert calls == []

    assert (await _run(db_session_factory, guild_id="123", apply=True, transport=transport))[0][
        "status"
    ] == "deleted"
    assert calls == ["https://discord.com/api/v10/guilds/123/roles/456"]
    assert await _run(db_session_factory, guild_id="123", apply=True, transport=transport) == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_purge_all_clears_missing_discord_roles(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(db_session, guild_id="123", role_id="456")
    await _seed(db_session, guild_id="789", role_id="987")
    await make_tenant(db_session, platform="slack", workspace_id="T123")
    await db_session.commit()

    transport = httpx.MockTransport(
        lambda request: httpx.Response(404 if request.url.path.endswith("/987") else 204)
    )
    rows = await _run(db_session_factory, all_workspaces=True, apply=True, transport=transport)
    assert [(row["workspace_id"], row["status"]) for row in rows] == [
        ("123", "deleted"),
        ("789", "already_absent"),
    ]
    assert await _run(db_session_factory, all_workspaces=True) == []


@pytest.mark.asyncio
async def test_failed_delete_keeps_row_for_retry(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(db_session, guild_id="123", role_id="456")
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    with pytest.raises(StoreError, match="rows were kept for retry"):
        await _run(db_session_factory, guild_id="123", apply=True, transport=transport)
    assert (await _run(db_session_factory, guild_id="123"))[0]["status"] == "would_delete"


@pytest.mark.asyncio
async def test_purge_rejects_invalid_scope(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(typer.BadParameter, match="only discord"):
        await roles_purge(
            rt=build_cli_runtime(db_session_factory, settings=_settings()),
            console=Console(file=StringIO()),
            platform="slack",
            workspace_id="123",
            all_workspaces=False,
            apply=False,
            as_json=True,
        )

"""Discord memory reads retain request bytes and ephemeral operator output."""

# pyright: reportPrivateUsage=false
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.memory_stores.beta_managed_agents_memory import BetaManagedAgentsMemory
from anthropic.types.beta.memory_stores.beta_managed_agents_memory_list_item import (
    BetaManagedAgentsMemoryListItem,
)
from daimon.adapters.discord import checks
from daimon.adapters.discord.commands import memory
from daimon.adapters.discord.errors import render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.mux_compat import list_memory_entries, read_memory
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from discord.ext import commands
from mux.contracts.ids import Scope


@pytest.mark.parametrize(
    "mode,status",
    [("list", 200), ("early", 200), ("late", 200), ("early", 403), ("late", 404), ("late", 409)],
)
async def test_memory_handler_bytes_pagination_early_break_and_errors(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    status: int,
) -> None:
    before, after = ScriptedTransport(), ScriptedTransport()
    for transport in (before, after):
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/memory_stores/store1/memories",
                httpx.Response(
                    200,
                    json={
                        "data": [
                            {"type": "memory_prefix", "path": "/notes/"},
                            {"type": "memory", "id": "m1", "path": "/a.md"},
                        ],
                        "next_page": "page2",
                        "has_more": True,
                    },
                ),
            )
        )
        if mode != "early":
            transport.queue(
                ScriptedReply(
                    "GET",
                    "/v1/memory_stores/store1/memories",
                    httpx.Response(
                        200,
                        json={
                            "data": [{"type": "memory", "id": "m2", "path": "/z.md"}],
                            "next_page": None,
                            "has_more": False,
                        },
                    ),
                )
            )
        if mode != "list":
            transport.queue(
                ScriptedReply(
                    "GET",
                    f"/v1/memory_stores/store1/memories/{'m1' if mode == 'early' else 'm2'}",
                    httpx.Response(
                        status,
                        json={
                            "content": "agent ``` text",
                            "id": "m1",
                            "path": "/a.md",
                            "type": "memory",
                            "memory_version_id": "v1",
                        }
                        if status == 200
                        else {"error": {"type": "permission_error", "message": "refused"}},
                    ),
                )
            )
    scopes: list[Scope] = []

    async def registered(*args: object, **kwargs: object):
        return derive_tenant_uuid(platform="discord", workspace_id="999")

    monkeypatch.setattr(checks, "resolve_tenant_for_interaction", registered)

    async def resolved(*args: object, **kwargs: object) -> tuple[str, str]:
        return "agent", "store1"

    async def walk(
        client: AsyncAnthropic, store_id: str, *, path_prefix: str, scope: Scope
    ) -> AsyncIterator[BetaManagedAgentsMemoryListItem]:
        scopes.append(scope)
        async for item in list_memory_entries(
            client, store_id, path_prefix=path_prefix, scope=scope
        ):
            yield item

    async def read(
        client: AsyncAnthropic, store_id: str, memory_id: str, *, scope: Scope
    ) -> BetaManagedAgentsMemory:
        scopes.append(scope)
        return await read_memory(client, store_id, memory_id, scope=scope)

    monkeypatch.setattr(memory, "_resolve_store", resolved)
    monkeypatch.setattr(memory, "list_memory_entries", walk)
    monkeypatch.setattr(memory, "read_memory", read)
    monkeypatch.setattr(memory, "generate_request_id", lambda: "fixed-request")
    path = None if mode == "list" else "/a.md" if mode == "early" else "/z.md"
    expected_error: Exception | None = None
    content = ""
    paths: list[str] = []
    async with before.client() as old, after.client() as new:
        try:
            page = await old.beta.memory_stores.memories.list("store1", path_prefix="/")
            async for item in page:
                if item.type == "memory":
                    if path is None:
                        paths.append(item.path)
                    elif item.path == path:
                        content = (
                            await old.beta.memory_stores.memories.retrieve(
                                item.id, memory_store_id="store1", view="full"
                            )
                        ).content or ""
                        break
        except Exception as exc:
            expected_error = exc
        runtime = MagicMock(spec=DiscordRuntime)
        runtime.anthropic = new
        interaction = MagicMock()
        interaction.client.runtime = runtime
        interaction.guild_id = 999
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        cog = memory.MemoryCog(cast(commands.Bot, MagicMock()))
        callback = cast(
            Callable[[memory.MemoryCog, memory.BotInteraction, str | None], Awaitable[None]],
            cog.memory.callback,
        )
        await callback(cog, cast(memory.BotInteraction, interaction), path)
    before.assert_consumed()
    after.assert_consumed()
    assert after.requests == before.requests
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.followup.send.assert_awaited_once()
    sent = interaction.followup.send.call_args
    assert sent.kwargs["ephemeral"] is True
    if expected_error is not None:
        assert sent.args[0] == render_error(expected_error, request_id="fixed-request")
    elif path is None:
        assert sent.args[0] == "**agent's memory** (2 files)\n- `/a.md`\n- `/z.md`"
    else:
        assert sent.args[0] == memory._fenced(f"**`{path}`**", content, memory._DISCORD_LIMIT)
    tenant = str(derive_tenant_uuid(platform="discord", workspace_id="999"))
    assert scopes and all(
        s.tenant_id == tenant and s.platform_reason is None and s.legacy_call_site is None
        for s in scopes
    )
    assert all(s is scopes[0] for s in scopes)

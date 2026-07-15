"""Tests for routines_panel.read + write — real-PG load + adapter wrappers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from daimon.adapters.discord.routines_panel.read import load_guild_routines
from daimon.adapters.discord.routines_panel.write import (
    pause_routine_via_panel,
    resume_routine_via_panel,
)
from daimon.core.cron import next_slot_at_or_after
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import SeedRoutineFn


def _handler_with(agents: list[dict[str, Any]]) -> Any:
    def _h(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": agents, "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    return _h


async def test_load_guild_routines_sorts_alphabetical_case_insensitive(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    seed_routine: SeedRoutineFn,
) -> None:
    await seed_routine(tenant_id=tenant_id, trigger_message="Zebra task")
    await seed_routine(tenant_id=tenant_id, trigger_message="alpha task")
    await seed_routine(tenant_id=tenant_id, trigger_message="Mango task")

    client = build_stub_anthropic(_handler_with([]))
    entries, over_cap_count, _ = await load_guild_routines(
        db_session,
        client,
        tenant_id=tenant_id,
    )
    labels = [e.label for e in entries]
    assert labels == ["alpha task", "Mango task", "Zebra task"], (
        "entries must be sorted alphabetical case-insensitive by label"
    )
    assert over_cap_count == 0, "three rows must not trip the 25-cap banner"


async def test_load_guild_routines_caps_at_25_with_over_cap_count(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    seed_routine: SeedRoutineFn,
) -> None:
    for i in range(30):
        await seed_routine(tenant_id=tenant_id, trigger_message=f"task {i:02d}")

    client = build_stub_anthropic(_handler_with([]))
    entries, over_cap_count, _ = await load_guild_routines(
        db_session,
        client,
        tenant_id=tenant_id,
    )
    assert len(entries) == 25, "load must cap entries at the Discord 25-option ceiling"
    assert over_cap_count == 5, "over_cap_count must report the truncated remainder"


async def test_load_guild_routines_empty_returns_empty_tuple(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    client = build_stub_anthropic(_handler_with([]))
    entries, over_cap_count, agent_name_map = await load_guild_routines(
        db_session,
        client,
        tenant_id=tenant_id,
    )
    assert entries == [], "empty DB must yield empty entries list"
    assert over_cap_count == 0
    assert agent_name_map == {}, "no agents stubbed means an empty agent_name_map"


async def test_load_guild_routines_glyph_per_row(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    seed_routine: SeedRoutineFn,
) -> None:
    fired = datetime(2026, 5, 1, tzinfo=UTC)
    await seed_routine(tenant_id=tenant_id, trigger_message="paused", enabled=False)
    await seed_routine(tenant_id=tenant_id, trigger_message="never")
    await seed_routine(
        tenant_id=tenant_id,
        trigger_message="errored",
        last_fired_at=fired,
        last_error="boom",
    )
    await seed_routine(
        tenant_id=tenant_id,
        trigger_message="success",
        last_fired_at=fired,
        last_result_tail="ok",
    )

    client = build_stub_anthropic(_handler_with([]))
    entries, _, _ = await load_guild_routines(
        db_session,
        client,
        tenant_id=tenant_id,
    )
    by_label = {e.label: e for e in entries}
    assert by_label["paused"].glyph == "⏸", "paused row must use ⏸"
    assert by_label["never"].glyph == "⏳", "never-run row must use ⏳"
    assert by_label["errored"].glyph == "❌", "errored row must use ❌"
    assert by_label["success"].glyph == "✅", "successful row must use ✅"


async def test_load_guild_routines_agent_name_fallback_for_missing_id(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    seed_routine: SeedRoutineFn,
) -> None:
    row = await seed_routine(tenant_id=tenant_id, agent_id="agent_unknown_id_12345678")
    client = build_stub_anthropic(_handler_with([]))
    entries, _, _ = await load_guild_routines(
        db_session,
        client,
        tenant_id=tenant_id,
    )
    assert len(entries) == 1
    expected = f"<agent {row.agent_id[:8]}>"
    assert entries[0].agent_name == expected, (
        "missing agent_id must fall back to a hex-prefix marker"
    )


async def test_load_guild_routines_label_fallback_for_blank_trigger(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    seed_routine: SeedRoutineFn,
) -> None:
    row = await seed_routine(tenant_id=tenant_id, trigger_message="   ")
    client = build_stub_anthropic(_handler_with([]))
    entries, _, _ = await load_guild_routines(
        db_session,
        client,
        tenant_id=tenant_id,
    )
    assert len(entries) == 1
    assert entries[0].label == f"routine {row.id.hex[:8]}", (
        "whitespace-only trigger_message must fall back to a hex-id label"
    )


async def test_pause_routine_via_panel_writes_atomically(
    db_session: AsyncSession,
    seed_routine: SeedRoutineFn,
) -> None:
    row = await seed_routine(
        enabled=True,
        next_fire_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    paused = await pause_routine_via_panel(db_session, row.id)
    assert paused is not None, "panel wrapper must return the updated row"
    assert paused.enabled is False, "wrapper must propagate enabled=False"
    assert paused.next_fire_at is None, "wrapper must clear next_fire_at"


async def test_resume_routine_via_panel_recomputes_next_fire_at(
    db_session: AsyncSession,
    seed_routine: SeedRoutineFn,
) -> None:
    row = await seed_routine(
        enabled=False,
        next_fire_at=None,
        cron_expr="*/5 * * * *",
        timezone="UTC",
    )
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    resumed = await resume_routine_via_panel(db_session, row.id, now=now)
    assert resumed is not None, "wrapper must return the updated row on resume"
    assert resumed.enabled is True
    expected_next = next_slot_at_or_after("*/5 * * * *", "UTC", now)
    assert resumed.next_fire_at == expected_next, (
        "wrapper must stamp next_fire_at via next_slot_at_or_after"
    )
    # Sanity: window-relative ordering
    assert resumed.next_fire_at is not None and resumed.next_fire_at > now - timedelta(seconds=1), (
        "computed next_fire_at must be at-or-after now"
    )

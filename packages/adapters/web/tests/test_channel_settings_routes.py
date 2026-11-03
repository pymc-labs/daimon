"""`/api/channels/{id}/settings`, its writes, and `/api/channel-defaults`."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path

import pytest
from daimon.adapters.web import channels as channels_module
from daimon.core import routine_service
from daimon.core.routine_service import RoutineRefused
from daimon.core.scope_cascade import CascadeMode, CascadeScope
from daimon.core.stores.routines import create_routine
from daimon.core.stores.switch_modes import set_switch_mode
from daimon.testing.turn_router import AGENT_ID
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import shown_trail
from .conftest import WebClientFactory
from .test_channels_routes import _channels  # pyright: ignore[reportPrivateUsage]

_WRITES: list[tuple[str, dict[str, object]]] = [
    ("/api/channels/c1/participation", {"mode": "on", "expected": None}),
    ("/api/channels/c1/progress-plans", {"mode": "on", "expected": None}),
    ("/api/channels/c1/environment", {"environment": None, "expected": None}),
    (
        "/api/channels/c1/tool-rules",
        {"agent": "a", "tool": "x/y", "rule": None, "expected_revision": 0},
    ),
    ("/api/channel-defaults/agent", {"agent": None, "expected": None}),
]


async def test_members_read_settings_but_never_write(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        admin.as_member()
        defaults = await admin.client.get("/api/channel-defaults")
        writes = [(await admin.client.put(url, json=body)).status_code for url, body in _WRITES]

    assert defaults.status_code == 200, defaults.text
    assert writes == [403] * len(_WRITES)


async def test_a_switch_is_compared_recorded_and_audited(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        first = await admin.client.put(
            "/api/channels/c1/progress-plans", json={"mode": "on", "expected": None}
        )
        stale = await admin.client.put(
            "/api/channels/c1/progress-plans", json={"mode": "off", "expected": None}
        )
        web = await admin.client.put(
            "/api/channels/web/participation", json={"mode": "on", "expected": None}
        )
        settings = (await admin.client.get("/api/channels/c1/settings")).json()
        history = (await admin.client.get("/api/channels/c1/history")).json()["entries"]
        trail = await shown_trail(db_session_factory, admin.ws.tenant_id)

    assert first.status_code == 200, first.text
    assert (stale.status_code, web.status_code) == (409, 422)
    assert settings["progress_plans"] == {
        "available": True,
        "own": "on",
        "value": "on",
        "source": "channel",
    }
    assert settings["link"] is not None and settings["link"].endswith("/c1")
    assert settings["tool_rules"]["editable"] is False, "tool safety is off here"
    assert [(h["setting"], h["after"]) for h in history] == [("progress_plans", "on")]
    assert trail[-1] == ("Changed channel", "#general", "Progress plans")


async def test_workspace_defaults_give_value_and_source_apart(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        before = (await admin.client.get("/api/channel-defaults")).json()
        put = await admin.client.put(
            "/api/channel-defaults/progress-plans", json={"mode": "on", "expected": None}
        )
        trail = await shown_trail(db_session_factory, admin.ws.tenant_id)

    assert set(before["agent"]) == {"value", "source"}
    assert put.status_code == 200, put.text
    assert put.json()["progress_plans"]["source"] == "workspace"
    assert trail[-1] == ("Changed channel", "Workspace", "Progress plans")


async def test_a_thread_override_lists_and_clears(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            await set_switch_mode(
                db,
                "progress_plans",
                tenant_id=admin.ws.tenant_id,
                platform="discord",
                scope=CascadeScope.THREAD,
                scope_id="t1",
                mode=CascadeMode.OFF,
                parent_id="c1",
            )
        listed = (await admin.client.get("/api/channels/c1/settings")).json()["threads"]
        cleared = await admin.client.post(
            "/api/channels/c1/threads/clear", json={"thread_id": "t1", "kind": "progress_plans"}
        )
        again = await admin.client.post(
            "/api/channels/c1/threads/clear", json={"thread_id": "t1", "kind": "progress_plans"}
        )
        after = (await admin.client.get("/api/channels/c1/settings")).json()["threads"]

    assert listed == [{"thread_id": "t1", "name": None, "kind": "progress_plans", "value": "off"}]
    assert (cleared.status_code, again.status_code) == (200, 409)
    assert after == []


async def test_a_members_drawer_opens_when_routines_cannot_be_checked(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def shown(
        _runtime: object, *, channel_ids: Collection[str], **_: object
    ) -> frozenset[str]:
        return frozenset(channel_ids)

    def refused(*_: object, **__: object) -> object:
        raise RoutineRefused("not available")

    monkeypatch.setattr(channels_module, "readable_channels", shown)
    monkeypatch.setattr(routine_service, "isolation_view_loader", refused)
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        admin.as_member()
        empty = await admin.client.get("/api/channels/c1/settings")
        async with db_session_factory.begin() as db:
            await create_routine(
                db,
                tenant_id=admin.ws.tenant_id,
                created_by_user_id=str(admin.member.id),
                agent_id=AGENT_ID,
                agent_name=DEFAULT_AGENT_NAME,
                cron_expr="0 9 * * *",
                timezone_="UTC",
                trigger_message="mine",
                next_fire_at=datetime(2030, 1, 1, tzinfo=UTC),
                destination_kind="channel",
                destination_id="c1",
                channel_id="c1",
            )
        refusal = await admin.client.get("/api/channels/c1/settings")

    assert empty.status_code == 200 and empty.json()["routines"] == []
    assert refusal.status_code == 200 and refusal.json()["routines"] == []


async def test_a_member_reads_no_spend_or_budget_and_only_the_routines_they_may_list(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def shown(
        _runtime: object, *, channel_ids: Collection[str], **_: object
    ) -> frozenset[str]:
        return frozenset(channel_ids)

    monkeypatch.setattr(channels_module, "readable_channels", shown)
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            for owner, message in ((str(admin.member.id), "mine"), ("someone-else", "theirs")):
                await create_routine(
                    db,
                    tenant_id=admin.ws.tenant_id,
                    created_by_user_id=owner,
                    agent_id=AGENT_ID,
                    agent_name=DEFAULT_AGENT_NAME,
                    cron_expr="0 9 * * *",
                    timezone_="UTC",
                    trigger_message=message,
                    next_fire_at=datetime(2030, 1, 1, tzinfo=UTC),
                    destination_kind="channel",
                    destination_id="c1",
                    channel_id="c1",
                )
        as_admin = (await admin.client.get("/api/channels/c1/settings")).json()
        admin.as_member()
        listed = (await admin.client.get("/api/channels")).json()["channels"]
        one = (await admin.client.get("/api/channels/c1")).json()
        settings = (await admin.client.get("/api/channels/c1/settings")).json()

    assert as_admin["spend"] is not None and len(as_admin["routines"]) == 2
    assert {r["id"] for r in listed} >= {"web", "c1"}, "the member lists the channels they can read"
    assert all(r["spend"] is None and r["budget"] is None for r in listed), "no figures in rows"
    assert one["spend"] is None and one["budget"] is None and one["budget_editable"] is False
    assert settings["spend"] is None, "a member reads nobody's spend"
    assert len(settings["routines"]) == 1, (
        "only the member's own routine, as the routine list shows"
    )


async def test_settings_name_the_agents_they_mention_and_a_missing_one_is_null(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            await create_routine(
                db,
                tenant_id=admin.ws.tenant_id,
                created_by_user_id=str(admin.member.id),
                agent_id=AGENT_ID,
                agent_name="ghost",
                cron_expr="0 9 * * *",
                timezone_="UTC",
                trigger_message="hi",
                next_fire_at=datetime(2030, 1, 1, tzinfo=UTC),
                destination_kind="channel",
                destination_id="c1",
                channel_id="c1",
            )
        settings = (await admin.client.get("/api/channels/c1/settings")).json()

    assert settings["agents"]["ghost"] == {"id": None, "name": None, "avatar": None}

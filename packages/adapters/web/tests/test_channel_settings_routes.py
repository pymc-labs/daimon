"""`/api/channels/{id}/settings`, its writes, and `/api/channel-defaults`."""

from __future__ import annotations

from pathlib import Path

from daimon.core.scope_cascade import CascadeMode, CascadeScope
from daimon.core.stores.switch_modes import set_switch_mode
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
        history = (await admin.client.get("/api/channels/c1/history")).json()
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

    assert listed == [{"thread_id": "t1", "kind": "progress_plans", "value": "off"}]
    assert (cleared.status_code, again.status_code) == (200, 409)
    assert after == []

"""`/api/channels/*`: who may read and change channels, stale values and the audit row."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
from daimon.adapters.web import channel_visibility
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.platform_names import ChannelSighting
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.platform_names import upsert_channel_names
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import shown_trail, signed_in, visitor_of
from .conftest import WebClientFactory
from .test_admin_routes import COOKIE, Admin, signed_in_admin

_ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("PUT", "/api/channels/c1/default-agent", {"agent": None, "expected": None}),
    ("POST", "/api/channels/c1/kept-agents", {"action": "add", "agent": "a", "expected": []}),
    (
        "PUT",
        "/api/channels/c1/access",
        {"readers": "inside", "expected": {"readers": "any", "writers": "any"}},
    ),
    ("PUT", "/api/channels/c1/budget", {"action": "clear", "expected": None}),
    ("GET", "/api/channels/c1/history", None),
]


@asynccontextmanager
async def _channels(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[Admin]:
    """The admin fixture with two named channels, one of them only its own agents read."""
    channel_visibility.forget_visibility()
    async with signed_in_admin(web_client, factory, tmp_path) as admin:
        tenant_id = admin.ws.tenant_id
        async with factory.begin() as db:
            await upsert_channel_names(
                db,
                tenant_id=tenant_id,
                platform="discord",
                names={"c1": ChannelSighting("general"), "c2": ChannelSighting("board")},
            )
            await set_access_policy(
                db,
                tenant_id=tenant_id,
                policy=TenantAccessPolicy(
                    channel_rules={"c2": ChannelRule(readers="own", writers="own")}
                ),
            )
        yield admin


async def test_admins_list_every_channel_and_members_only_what_they_can_read(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        listed = (await admin.client.get("/api/channels")).json()
        board = (await admin.client.get("/api/channels/c2")).json()
        admin.as_member()
        member = (await admin.client.get("/api/channels")).json()
        hidden = await admin.client.get("/api/channels/c1")
        refused = {
            path: (await admin.client.request(method, path, json=body)).status_code
            for method, path, body in _ROUTES
        }

    ids = [row["id"] for row in listed["channels"]]
    assert ids == ["web", "c2", "c1"], "web chat first, then by name"
    assert listed["channels"][0]["name"] == "Web chat"
    assert board["tags"] == ["locked"] and board["can_manage"] is True
    assert board["readers_label"] == "Only its own agents"
    assert [row["id"] for row in member["channels"]] == ["web"], (
        "a member whose chat account cannot be checked sees no channel (fail closed)"
    )
    assert hidden.status_code == 404
    assert set(refused.values()) == {403}, refused


async def test_visitors_are_refused(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        visitor = await visitor_of(db_session_factory, admin.ws)
        admin.client.cookies.set(
            COOKIE, await signed_in(db_session_factory, visitor), domain="web.example.com"
        )
        listed = await admin.client.get("/api/channels")
        one = await admin.client.get("/api/channels/c1")

    assert (listed.status_code, one.status_code) == (403, 403)


async def test_a_stale_value_is_a_409_and_a_change_is_audited_and_in_history(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    budget = {"action": "set", "limit_usd": "10", "window": "monthly"}
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        stale_default = await admin.client.put(
            "/api/channels/c1/default-agent", json={"agent": None, "expected": "someone"}
        )
        first = await admin.client.put("/api/channels/c1/budget", json={**budget, "expected": None})
        stale_budget = await admin.client.put(
            "/api/channels/c1/budget", json={**budget, "limit_usd": "20", "expected": None}
        )
        web_budget = await admin.client.put(
            "/api/channels/web/budget", json={**budget, "expected": None}
        )
        history = (await admin.client.get("/api/channels/c1/history")).json()
        trail = await shown_trail(db_session_factory, admin.ws.tenant_id)

    assert stale_default.status_code == 409, stale_default.text
    assert first.status_code == 200, first.text
    assert first.json()["channel"]["budget"]["limit_usd"] == "10.00"
    assert stale_budget.status_code == 409, "the budget moved since it was read"
    assert web_budget.status_code == 422
    assert [(h["setting"], h["surface"], h["actor"]["name"]) for h in history] == [
        ("budget", "web", "Ada")
    ]
    assert trail[-1] == ("Changed channel", "#general", "Budget")


async def test_the_member_list_needs_the_module(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        off = await admin.client.patch("/api/admin/settings", json={"modules": {"channels": False}})
        admin.as_member()
        listed = await admin.client.get("/api/channels")

    assert off.status_code == 200, off.text
    assert listed.status_code == 403, "a module turned off is closed to members"


@dataclass
class _Discord:
    bot_token: SecretStr = SecretStr("token")


@dataclass
class _Settings:
    discord: _Discord


@dataclass
class _Runtime:
    """What `readable_channels` reads of the runtime, with every request failing."""

    settings: _Settings
    http_client: httpx.AsyncClient


async def test_a_platform_that_cannot_be_checked_shows_no_channel() -> None:
    channel_visibility.forget_visibility()
    failing = httpx.MockTransport(lambda _: httpx.Response(500))
    async with httpx.AsyncClient(transport=failing) as http:
        runtime = cast("WebRuntime", _Runtime(_Settings(_Discord()), http))
        found = {
            platform: await channel_visibility.readable_channels(
                runtime,
                tenant_id=uuid.uuid4(),
                platform=platform,
                workspace_id="w1",
                user_id="u1",
                channel_ids=("c1",),
            )
            for platform in ("discord", "teams")
        }

    assert found == {"discord": frozenset(), "teams": frozenset()}, "fail closed"

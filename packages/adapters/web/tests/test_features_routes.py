"""`/api/features`: the schema, a place's features, and a change at each tier."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest
from daimon.adapters.web import channels as channels_module
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import thread_open_to_anyone
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role as AccountRole
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_thread_session
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, agent_client, agent_people, sign_in_token
from .conftest import WebClientFactory
from .test_channels_routes import _channels  # pyright: ignore[reportPrivateUsage]


def _tier(response: Response, key: str, tier: str) -> str | None:
    """A tier's own value text for `key`, or None when the place lacks that tier."""
    features: list[dict[str, Any]] = response.json()["features"]
    (found,) = [f for f in features if f["key"] == key]
    own: list[dict[str, Any]] = found["tiers"]
    return next((t["text"] for t in own if t["tier"] == tier), None)


async def test_the_schema_lists_every_registered_feature(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        schema = (await admin.client.get("/api/features/schema")).json()
    assert [f["key"] for f in schema] == list(FEATURES)


async def test_an_admin_changes_a_channel_and_a_stale_read_is_refused(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        url = "/api/features?channel=c1"
        body = {"key": "cost_footer", "tier": "channel", "value": "hidden", "expected": "default"}
        first = await admin.client.put(url, json=body)
        stale = await admin.client.put(url, json={**body, "value": "shown"})
        read = await admin.client.get(url)
        thread = await admin.client.get("/api/features?channel=c1&thread=t9")
        no_channel = await admin.client.get("/api/features?thread=t9")
        web = await admin.client.get("/api/features?channel=web")

    assert first.status_code == 200, first.text
    assert first.json()["value"] == "hidden" and first.json()["source"] == "channel"
    assert stale.status_code == 409
    assert _tier(read, "cost_footer", "channel") == "hidden"
    assert _tier(read, "cost_footer", "thread") is None
    assert _tier(thread, "cost_footer", "thread") == "default"
    assert (no_channel.status_code, web.status_code) == (422, 422)


async def test_a_thread_is_changed_only_if_it_is_there(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A thread the workspace has no session in, or one in another channel, is not found."""
    body = {"key": "progress_plans", "tier": "thread", "value": "on"}
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            tenant = await get_tenant(db, admin.ws.tenant_id)
            assert tenant is not None, "the signed-in workspace exists"
            await make_thread_session(
                db, tenant=tenant, platform="discord", thread_id="t1", channel_id="c1"
            )
        ok = await admin.client.put("/api/features?channel=c1&thread=t1", json=body)
        nonexistent = await admin.client.put("/api/features?channel=c1&thread=t9", json=body)
        other_channel = await admin.client.put("/api/features?channel=c2&thread=t1", json=body)

    assert ok.status_code == 200, ok.text
    assert nonexistent.status_code == 404, "a thread with no session is not found"
    assert other_channel.status_code == 404, "a thread of another channel is not found"


@pytest.mark.parametrize(
    ("key", "locked"),
    [("progress_plans", False), ("progress_plans", True), ("cost_footer", False)],
)
async def test_a_member_sets_a_thread_value_where_the_core_rule_opens_it(
    key: str,
    locked: bool,
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As in chat: a member who can read the channel sets a thread value when
    `thread_open_to_anyone` holds, and is refused for any other feature or a Lock. The core
    test covers every feature; this checks the route follows it."""

    async def shown(
        _runtime: object, *, channel_ids: Collection[str], **_: object
    ) -> frozenset[str]:
        return frozenset(channel_ids)

    monkeypatch.setattr(channels_module, "readable_channels", shown)
    value = next(v for v in FEATURES[key].values if v != FEATURES[key].default)
    body = {"key": key, "tier": "thread", "value": value, "locked": locked}
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            tenant = await get_tenant(db, admin.ws.tenant_id)
            assert tenant is not None, "the signed-in workspace exists"
            await make_thread_session(
                db, tenant=tenant, platform="discord", thread_id="t1", channel_id="c1"
            )
        admin.as_member()
        member = await admin.client.put("/api/features?channel=c1&thread=t1", json=body)

    if thread_open_to_anyone(key, locked=locked):
        assert member.status_code == 200, f"{key}: the rule opens it to a member: {member.text}"
    else:
        assert member.status_code in (400, 403, 409, 422), (
            f"{key}: a member must be refused, got {member.status_code}: {member.text}"
        )


async def test_members_read_but_cannot_change_the_workspace_or_a_channel(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        admin.as_member()
        read = await admin.client.get("/api/features")
        workspace = await admin.client.put(
            "/api/features", json={"key": "cost_footer", "tier": "workspace", "value": "hidden"}
        )
        channel = await admin.client.put(
            "/api/features?channel=c1",
            json={"key": "progress_plans", "tier": "channel", "value": "on"},
        )
    assert read.status_code == 200, read.text
    assert workspace.status_code == 403
    assert channel.status_code in (403, 404), "a channel a member cannot read is not found"


async def test_an_owner_turns_a_built_in_tool_off_on_their_agent_and_others_cannot(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    body = {"key": "tool.bash", "tier": "agent", "value": "off"}
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, await sign_in_token(db_session_factory, owner)
    ) as client:
        read = await client.get("/api/features?agent=ag_w")
        made = await client.put("/api/features?agent=ag_w", json=body, headers=OWN)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, await sign_in_token(db_session_factory, other)
    ) as client:
        refused = await client.put("/api/features?agent=ag_w", json=body, headers=OWN)

    assert read.status_code == 200, read.text
    assert _tier(read, "tool.bash", "agent") is not None
    assert made.status_code == 200, made.text
    assert len(agents.updates) == 1 and "tools" in agents.updates[0]
    assert refused.status_code == 403
    assert len(agents.updates) == 1, "a person who does not own the agent changes nothing"


async def test_a_workspace_owner_cannot_change_long_jobs_but_a_server_admin_can(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A feature with no delegated action is a server admin's alone: the web workspace
    owner is refused, the same person is allowed once their account is a server admin."""
    bodies = [
        {"key": "long_jobs", "tier": "workspace", "value": "on"},
        {"key": "long_jobs_max_usd", "tier": "workspace", "value": "20"},
        {"key": "long_jobs_max_deadline", "tier": "workspace", "value": "30"},
    ]
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        refused = [await admin.client.put("/api/features", json=body) for body in bodies]
        async with db_session_factory.begin() as db:
            await set_role(db, admin.ws.person.account_id, AccountRole.ADMIN)
        allowed = [await admin.client.put("/api/features", json=body) for body in bodies]
        ordinary = await admin.client.put(
            "/api/features", json={"key": "routines", "tier": "workspace", "value": "off"}
        )
    assert [r.status_code for r in refused] == [403, 403, 403], [r.text for r in refused]
    assert [r.status_code for r in allowed] == [200, 200, 200], [r.text for r in allowed]
    assert ordinary.status_code == 200, "a delegated action still passes for an owner"


async def test_an_agent_owner_cannot_change_long_jobs_on_their_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _ = await agent_people(db_session)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, await sign_in_token(db_session_factory, owner)
    ) as client:
        usd = await client.put(
            "/api/features?agent=ag_w",
            json={"key": "long_jobs_max_usd", "tier": "agent", "value": "20"},
            headers=OWN,
        )
        on = await client.put(
            "/api/features?agent=ag_w",
            json={"key": "long_jobs", "tier": "agent", "value": "on"},
            headers=OWN,
        )
    assert (usd.status_code, on.status_code) == (403, 403), "the owner path is closed"

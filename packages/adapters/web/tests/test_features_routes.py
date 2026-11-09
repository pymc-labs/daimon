"""`/api/features`: the schema, a place's features, and a change at each tier."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from daimon.core.features.registry import FEATURES
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
        body = {"key": "progress_plans", "tier": "channel", "value": "on", "expected": "default"}
        first = await admin.client.put(url, json=body)
        stale = await admin.client.put(url, json={**body, "value": "off"})
        read = await admin.client.get(url)
        thread = await admin.client.get("/api/features?channel=c1&thread=t9")
        no_channel = await admin.client.get("/api/features?thread=t9")
        web = await admin.client.get("/api/features?channel=web")

    assert first.status_code == 200, first.text
    assert first.json()["value"] == "on" and first.json()["source"] == "channel"
    assert stale.status_code == 409
    assert _tier(read, "progress_plans", "channel") == "on"
    assert _tier(read, "progress_plans", "thread") is None
    assert _tier(thread, "progress_plans", "thread") == "default"
    assert (no_channel.status_code, web.status_code) == (422, 422)


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

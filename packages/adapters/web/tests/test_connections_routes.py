"""`/api/integrations/connections`: admins only, a tool turns on as shown, and no credential."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from daimon.core.config import Settings
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores import mcp_connections as store
from daimon.core.stores.domain import DiscoveredTool
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import Workspace, shown_trail
from .conftest import WebClientFactory
from .test_integrations_routes import Rig

_PATH = "/api/integrations/connections"
_KEYED = "https://mcp.example.com/mcp?key=hunter2"


@pytest.fixture
def rig(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Rig:
    return Rig(db_session_factory, tmp_path, web_client, monkeypatch)


async def _seed(rig: Rig, ws: Workspace, *, feature: str = "on") -> uuid.UUID:
    schema: dict[str, JsonValue] = {"type": "object", "properties": {"id": {"type": "string"}}}
    async with rig.factory.begin() as db:
        row = await store.create_connection(
            db,
            tenant_id=ws.tenant_id,
            name="crm",
            url=_KEYED,
            encrypted_auth=b"sealed",
            scope=None,
        )
        await store.sync_tools(
            db,
            tenant_id=ws.tenant_id,
            connection_id=row.id,
            discovered=[
                DiscoveredTool(
                    name="lookup", description="Finds one.", input_schema=schema, writes=False
                )
            ],
        )

    async def admin(_: object) -> bool:
        return True

    await change_feature(
        rig.factory,
        Settings(_env_file=None, database={"url": "postgresql+asyncpg://u:p@h/db"}),  # pyright: ignore[reportCallIssue, reportArgumentType]
        FeatureActor(ws.tenant_id, None, "web", None, admin),
        key="shared_connections",
        tier="workspace",
        value=feature,
        place=FeaturePlace(ws.tenant_id, "discord"),
    )
    return row.id


@pytest.mark.parametrize("who", ["member", "visitor"])
async def test_members_and_visitors_are_refused(rig: Rig, who: str) -> None:
    """Only an admin or owner reads or changes a workspace's shared connections."""
    async with rig.open(as_=who) as (client, ws):
        await _seed(rig, ws)
        for method, path in (("GET", _PATH), ("DELETE", f"{_PATH}/{uuid.uuid4()}")):
            res = await client.request(method, path)
            assert res.status_code == 403, f"{method} {path} is closed to a {who}: {res.text}"


async def test_a_tool_turns_on_as_shown_and_the_credential_never_appears(rig: Rig) -> None:
    """Approval needs the fingerprint of what was shown; the answer holds no token or query."""
    async with rig.open(as_="admin") as (client, ws):
        connection = await _seed(rig, ws)
        listing = await client.get(_PATH)
        tool = listing.json()["connections"][0]["tools"][0]
        path = f"{_PATH}/{connection}/tools/lookup"
        stale = await client.patch(path, json={"enabled": True, "seen": "0" * 16})
        shown = await client.patch(path, json={"enabled": True, "seen": tool["seen"]})
        trail = await shown_trail(rig.factory, ws.tenant_id)

    assert "hunter2" not in listing.text and "sealed" not in listing.text, "no credential in sight"
    assert listing.json()["connections"][0]["has_token"] is True, "the page says one is stored"
    assert stale.status_code == 409, f"a fingerprint that isn't what was shown is refused: {stale}"
    assert shown.json()["tools"][0]["enabled"] is True, (
        f"the shown fingerprint turns it on: {shown}"
    )
    assert [a for a, _, _ in trail] == ["Changed shared connection tool"], f"one audit row: {trail}"


async def test_nothing_changes_while_the_feature_is_off(rig: Rig) -> None:
    """With the feature off the page still lists, but a change is refused."""
    async with rig.open(as_="admin") as (client, ws):
        connection = await _seed(rig, ws, feature="off")
        listing = await client.get(_PATH)
        refused = await client.patch(f"{_PATH}/{connection}", json={"status": "paused"})

    assert listing.json()["enabled"] is False, "the page is told the feature is off"
    assert refused.status_code == 409, f"a change needs the feature on: {refused.status_code}"

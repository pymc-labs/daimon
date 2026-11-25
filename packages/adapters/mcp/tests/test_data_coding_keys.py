"""A coding key reads data as its maker, held to the maker's standing and channel rule."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from daimon.adapters.mcp import data_access
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._hub_workspace import pick_workspace
from daimon.core.data_audience import (
    DataOrigin,
    DataRefusal,
    Reader,
    data_audience,
    scope_ceiling,
    user_subject,
)
from daimon.core.hub_identity import HubTenant
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError

_KEY = {
    "platform": "discord",
    "platform_user_id": "7",
    "external_id": "1",
    "agent_id": uuid.uuid4(),
    "coding_key": True,
}
_NO_RUNTIME = cast(McpRuntime, None)


def _auth(**fields: Any) -> AuthIdentity:
    return AuthIdentity(account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, **fields)


async def test_a_coding_key_never_reads_past_its_maker() -> None:
    maker = _auth(**_KEY)
    origin = await data_access.data_origin(_NO_RUNTIME, maker, now=datetime.now(UTC))
    owner = uuid.uuid4()
    audience = data_audience(origin, agent_owner=owner)
    assert not isinstance(audience, DataRefusal)
    assert maker.account_id in audience.people, "the key's audience includes its maker"
    held = {maker.account_id: frozenset({"team", "docs"}), owner: frozenset({"docs", "other"})}
    assert scope_ceiling(audience, held) == {"docs"}, "scopes beyond the maker's are never read"


async def test_a_key_that_is_not_a_coding_key_reads_nothing() -> None:
    origin = await data_access.data_origin(
        _NO_RUNTIME, _auth(**{**_KEY, "coding_key": False}), now=datetime.now(UTC)
    )
    assert isinstance(data_audience(origin, agent_owner=None), DataRefusal)


async def test_a_key_minted_in_a_limited_channel_keeps_that_channels_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def room(
        runtime: McpRuntime,
        auth: AuthIdentity,
        asker: Reader,
        channel_id: str,
        thread_id: str | None,
        *,
        now: datetime,
    ) -> DataOrigin:
        seen.append(channel_id)
        return DataOrigin("room", asker, contained=True)

    monkeypatch.setattr(data_access, "_room", room)
    auth = _auth(**_KEY, bound_channel_id="c9")
    origin = await data_access.data_origin(_NO_RUNTIME, auth, now=datetime.now(UTC))
    assert seen == ["c9"], "the channel the key was minted in decides"
    assert isinstance(data_audience(origin, agent_owner=uuid.uuid4()), DataRefusal)
    assert origin.asker == Reader(user_subject("discord", "1", "7"), auth.account_id)


def test_a_hub_caller_picks_among_their_own_workspaces_only() -> None:
    from daimon.adapters.mcp.hub.identity import HubIdentity

    mine = HubTenant(
        tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), workspace_id="w1", workspace_name="Alpha"
    )
    other = HubTenant(
        tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), workspace_id="w2", workspace_name="Beta"
    )
    two = HubIdentity(platform="discord", platform_user_id="7", tenants=(mine, other))
    assert pick_workspace(two, "alpha") is mine
    with pytest.raises(ToolError):
        pick_workspace(two, None)  # several workspaces: the caller must pick
    with pytest.raises(ToolError):
        pick_workspace(two, "w3")  # not a member there
    one = HubIdentity(platform="discord", platform_user_id="7", tenants=(mine,))
    assert pick_workspace(one, None) is mine

"""A pinned agent posts only into its pinned channels, wherever its turn ran.

An admin's DM or hub turn is exempt from a pin, so a pinned agent can run
there. Its channel sends (`require_channel_writable`, called by every
Discord, Slack and Teams send path) still reach only its pinned channels and
threads under them, so its context never lands in another channel.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_channel_writable
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AGENT = "ag_acme"

# (platform, pinned channel, in-pin target, in-pin parent, outside target, outside parent)
_SHAPES: list[tuple[str, str, str, str | None, str, str | None]] = [
    ("discord", "111", "111", None, "999", None),
    ("discord", "111", "thread-5", "111", "thread-6", "999"),
    ("slack", "C111", "C111", None, "C999", None),
    (
        "teams",
        "19:acme@thread.tacv2",
        "19:acme@thread.tacv2;messageid=1",
        "19:acme@thread.tacv2",
        "19:other@thread.tacv2",
        "19:other@thread.tacv2",
    ),
]


async def _runtime(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], *, platform: str, pin: str
) -> tuple[McpRuntime, uuid.UUID]:
    tenant = await make_tenant(db, platform=platform, workspace_id=f"ws-{uuid.uuid4()}")  # pyright: ignore[reportArgumentType]
    await set_access_policy(
        db,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"acme-project": (pin,)}),
    )
    await db.commit()
    router = MARouter()
    agent: dict[str, Any] = ma_agent(
        id=_AGENT,
        name="Acme Display",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "acme-project"},
    ).model_dump(mode="json")
    other: dict[str, Any] = ma_agent(
        id="ag_other",
        name="other",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "other"},
    ).model_dump(mode="json")
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent, other]))
    settings = MagicMock()
    return (
        McpRuntime(
            session_factory=sessionmaker,
            client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
            settings=settings,  # type: ignore[arg-type]
            deployment_default=DeploymentDefault(),
        ),
        tenant.id,
    )


def _turn(tenant_id: uuid.UUID, platform: str, *, agent_key: bool = False) -> AuthIdentity:
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_AGENT)
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.ADMIN,
        platform=platform,
        platform_user_id="u1",
        is_admin=True,
        **({"agent_id": agent_uuid} if agent_key else {"chat_agent_id": agent_uuid}),
    )


@pytest.mark.parametrize("agent_key", [False, True], ids=["chat-turn", "agent-key"])
@pytest.mark.parametrize(
    ("platform", "pin", "inside", "inside_parent", "outside", "outside_parent"), _SHAPES
)
async def test_a_pinned_agent_posts_only_into_its_pinned_channels(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: str,
    pin: str,
    inside: str,
    inside_parent: str | None,
    outside: str,
    outside_parent: str | None,
    agent_key: bool,
) -> None:
    runtime, tenant_id = await _runtime(db_session, db_session_factory, platform=platform, pin=pin)
    auth = _turn(tenant_id, platform, agent_key=agent_key)

    await require_channel_writable(
        runtime, auth, channel_id=inside, parent_channel_id=inside_parent
    )
    with pytest.raises(ToolError, match="pinned to its own channels"):
        await require_channel_writable(
            runtime, auth, channel_id=outside, parent_channel_id=outside_parent
        )


async def test_unpinned_agents_and_the_operator_post_anywhere_unknown_agents_fail_closed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, tenant_id = await _runtime(
        db_session, db_session_factory, platform="discord", pin="111"
    )
    operator = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    await require_channel_writable(runtime, operator, channel_id="999")

    unpinned = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        platform="discord",
        chat_agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_other"),
    )
    await require_channel_writable(runtime, unpinned, channel_id="999")

    other_agent = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        platform="discord",
        chat_agent_id=uuid.uuid4(),
    )
    with pytest.raises(ToolError, match="pinned to its own channels"):
        # An agent that can't be resolved while pins exist fails closed.
        await require_channel_writable(runtime, other_agent, channel_id="999")

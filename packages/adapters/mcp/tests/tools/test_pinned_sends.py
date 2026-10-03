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
from daimon.adapters.mcp.tools._channel_policy import (
    require_channel_writable,
    require_dm_recipient_allowed,
)
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
    with pytest.raises(ToolError, match="runs it only in certain channels"):
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
    with pytest.raises(ToolError, match="runs it only in certain channels"):
        # An agent that can't be resolved while pins exist fails closed.
        await require_channel_writable(runtime, other_agent, channel_id="999")


@pytest.mark.parametrize(
    ("platform", "dm"), [("slack", "D0ADMIN1"), ("teams", "a:1adminpersonalchat")]
)
async def test_a_pinned_agent_answers_in_the_requesters_own_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: str,
    dm: str,
) -> None:
    """An admin's exempt DM turn can post there -- credential cards included."""
    pin = "C111" if platform == "slack" else "19:acme@thread.tacv2"
    runtime, tenant_id = await _runtime(db_session, db_session_factory, platform=platform, pin=pin)

    await require_channel_writable(runtime, _turn(tenant_id, platform), channel_id=dm)


@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_a_pinned_agent_direct_messages_only_the_requester(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: str,
) -> None:
    import dataclasses

    from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl

    pin, me, other = (
        ("111", "1001", "1002") if platform == "discord" else ("C111", "U1001", "U1002")
    )
    runtime, tenant_id = await _runtime(db_session, db_session_factory, platform=platform, pin=pin)
    auth = dataclasses.replace(_turn(tenant_id, platform), platform_user_id=me)

    with pytest.raises(ToolError, match="only send a direct message to the person"):
        await send_direct_message_impl(runtime, auth, recipient_id=other, content="acme terms")
    await require_dm_recipient_allowed(runtime, auth, recipient_id=me)


async def test_direct_messages_from_unpinned_agents_pass_and_unknown_agents_fail_closed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, tenant_id = await _runtime(
        db_session, db_session_factory, platform="slack", pin="C111"
    )
    unpinned = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        platform="slack",
        platform_user_id="u1",
        chat_agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_other"),
    )
    await require_dm_recipient_allowed(runtime, unpinned, recipient_id="u2")

    unknown = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        platform="slack",
        platform_user_id="u1",
        chat_agent_id=uuid.uuid4(),
    )
    with pytest.raises(ToolError, match="runs it only in certain channels"):
        await require_dm_recipient_allowed(runtime, unknown, recipient_id="u2")


class _FakeSlack:
    """The Slack calls a card post and a send_message make, nothing else."""

    def __init__(self, channel: dict[str, Any]) -> None:
        self.channel = channel
        self.posted: list[str] = []

    async def conversations_info(self, *, channel: str) -> dict[str, Any]:
        return {"channel": {**self.channel, "id": channel}}

    async def users_info(self, *, user: str) -> dict[str, Any]:
        return {"user": {"id": user}}

    async def conversations_members(self, **_: Any) -> dict[str, Any]:
        return {"members": [], "response_metadata": {}}

    async def chat_postMessage(self, *, channel: str, **_: Any) -> Any:
        self.posted.append(channel)
        data = {"ts": "1700000000.000100", "message": {"user": "UBOT"}}
        return type("_Resp", (dict,), {"data": data})(data)


async def _slack_admin(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: Any, im_user: str
) -> tuple[McpRuntime, AuthIdentity, _FakeSlack]:
    import dataclasses

    from daimon.adapters.mcp.tools.slack import _credential_button, _send

    runtime, tenant_id = await _runtime(db, sessionmaker, platform="slack", pin="C111")
    auth = dataclasses.replace(
        _turn(tenant_id, "slack"), platform_user_id="U1001", external_id="T1"
    )
    fake = _FakeSlack({"is_im": True, "user": im_user})

    async def client(*_a: Any, **_k: Any) -> _FakeSlack:
        return fake

    monkeypatch.setattr(_credential_button, "slack_web_client", client)
    monkeypatch.setattr(_send, "slack_web_client", client)
    return runtime, auth, fake


async def _post_card(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
    from datetime import UTC, datetime, timedelta

    from daimon.adapters.mcp.tools.slack._credential_button import (
        _post_slack_credential_button_impl,  # pyright: ignore[reportPrivateUsage]
    )

    return await _post_slack_credential_button_impl(
        runtime,
        auth,
        channel_id=channel_id,
        kind="env",
        target="CRM_TOKEN",
        token="tok",
        agent_name="acme-project",
        purpose="crm",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        responder_name="acme-project",
    )


async def test_a_pinned_agent_posts_a_credential_card_and_a_reply_in_the_admins_own_slack_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.mcp.tools.slack._send import (
        _slack_send_message_impl,  # pyright: ignore[reportPrivateUsage]
    )

    runtime, auth, fake = await _slack_admin(
        db_session, db_session_factory, monkeypatch, im_user="U1001"
    )

    await _post_card(runtime, auth, "D0ADMIN1")
    await _slack_send_message_impl(
        runtime, auth, channel_id="D0ADMIN1", content="done", attachments=None, file_handles=None
    )

    assert fake.posted == ["D0ADMIN1", "D0ADMIN1"]


async def test_a_pinned_agent_never_posts_into_someone_elses_slack_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.mcp.tools.slack._send import (
        _slack_send_message_impl,  # pyright: ignore[reportPrivateUsage]
    )

    runtime, auth, fake = await _slack_admin(
        db_session, db_session_factory, monkeypatch, im_user="U1002"
    )

    with pytest.raises(ToolError):
        await _post_card(runtime, auth, "D0OTHER1")
    with pytest.raises(ToolError):
        await _slack_send_message_impl(
            runtime,
            auth,
            channel_id="D0OTHER1",
            content="acme terms",
            attachments=None,
            file_handles=None,
        )
    assert fake.posted == []


async def test_every_pinned_name_binds_the_send(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Outside the pin of ANY name the agent answers to is refused, as in admission."""
    runtime, tenant_id = await _runtime(
        db_session, db_session_factory, platform="slack", pin="C111"
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant_id,
        policy=TenantAccessPolicy(
            agent_channel_pins={"acme-project": ("C111",), "Acme Display": ("C222",)}
        ),
    )
    await db_session.commit()
    auth = _turn(tenant_id, "slack")

    for channel in ("C111", "C222"):
        with pytest.raises(ToolError, match="runs it only in certain channels"):
            await require_channel_writable(runtime, auth, channel_id=channel)


async def _with_pins(
    db: AsyncSession, tenant_id: uuid.UUID, pins: dict[str, tuple[str, ...]]
) -> None:
    await set_access_policy(
        db, tenant_id=tenant_id, policy=TenantAccessPolicy(agent_channel_pins=pins)
    )
    await db.commit()


@pytest.mark.parametrize("agent_key", [False, True], ids=["chat-turn", "agent-key"])
async def test_overlapping_alias_pins_bind_the_slack_send_to_their_intersection(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    agent_key: bool,
) -> None:
    """Each name's pin must allow the target; a wider alias pin never adds a channel."""
    import dataclasses

    from daimon.adapters.mcp.tools.slack import _send

    runtime, tenant_id = await _runtime(
        db_session, db_session_factory, platform="slack", pin="C111"
    )
    await _with_pins(
        db_session, tenant_id, {"Acme Display": ("C111",), "acme-project": ("C111", "C999")}
    )
    auth = dataclasses.replace(
        _turn(tenant_id, "slack", agent_key=agent_key),
        platform_user_id="U1001",
        external_id="T1",
    )
    fake = _FakeSlack({"is_im": False, "is_private": False})

    async def client(*_a: Any, **_k: Any) -> _FakeSlack:
        return fake

    monkeypatch.setattr(_send, "slack_web_client", client)

    await _send._slack_send_message_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, auth, channel_id="C111", content="ok", attachments=None, file_handles=None
    )
    with pytest.raises(ToolError, match="runs it only in certain channels"):
        await _send._slack_send_message_impl(  # pyright: ignore[reportPrivateUsage]
            runtime,
            auth,
            channel_id="C999",
            content="private acme terms",
            attachments=None,
            file_handles=None,
        )
    assert fake.posted == ["C111"]


async def test_an_empty_stored_pin_fails_closed_for_sends_and_dms(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    import dataclasses

    runtime, tenant_id = await _runtime(
        db_session, db_session_factory, platform="slack", pin="C111"
    )
    await _with_pins(db_session, tenant_id, {"acme-project": ()})
    auth = dataclasses.replace(_turn(tenant_id, "slack"), platform_user_id="U1001")

    for channel in ("C111", "C999"):
        with pytest.raises(ToolError, match="runs it only in certain channels"):
            await require_channel_writable(runtime, auth, channel_id=channel)
    with pytest.raises(ToolError, match="only send a direct message to the person"):
        await require_dm_recipient_allowed(runtime, auth, recipient_id="U1002")
    await require_dm_recipient_allowed(runtime, auth, recipient_id="U1001")

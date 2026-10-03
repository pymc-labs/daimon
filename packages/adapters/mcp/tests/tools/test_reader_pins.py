"""A published report's reader variant stays under its source agent's pin.

Model gap G_pin_report_reader: pins match an agent by name, and a reader
variant is named ``<source>-reader``, so without this a member could run a
pinned agent's reader off-channel. The reader carries its source's names
(`agent_pin_names`), and publishing a pinned source's reader is a
configuration change decided by the pinned-agent write rule.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.publish import (
    _publish_report_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import agent_pin_names
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.reports.host_client import Recipient
from daimon.core.reports.publish import PublishResult
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent
from daimon.testing.factories import make_account
from daimon.testing.ma import MARouter, build_fake_anthropic
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_pin_guard import _AGENT_ID, _member, _pinned

_PINS = TenantAccessPolicy(agent_channel_pins={"acme-config": ("C_ACME",)})


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "daimon_name": "acme-config-reader",
            "daimon_reader_of": "fp",
            "daimon_reader_source": "acme-config\nAcme Display",
        },
        # A reader written before the source stamp: its name minus the suffix.
        {"daimon_name": "acme-config-reader", "daimon_reader_of": "fp"},
    ],
    ids=["stamped", "legacy"],
)
def test_a_reader_answering_off_channel_is_refused_by_its_sources_pin(
    metadata: dict[str, str],
) -> None:
    names = agent_pin_names("acme-config-reader", metadata)
    decision = authorize(
        _PINS,
        subject=Subject(platform_user_id="member-1"),
        action=Action.RUN_AGENT,
        surface=Surface.AGENT_CHAT,
        agent=AgentRef.of(*names),
        place=Place(),
    )
    assert decision.reason == "agent_pinned_elsewhere"


def test_an_ordinary_agent_named_like_a_reader_gains_no_extra_pin() -> None:
    assert agent_pin_names("x-reader", {"daimon_name": "x-reader"}) == ("x-reader", "x-reader")


def _with_helper(runtime: McpRuntime, tenant_id: uuid.UUID) -> McpRuntime:
    """`runtime` whose tenant also has ``helper``, an unpinned agent a chat turn runs."""
    router = MARouter()
    router.add_agent_list(
        ma_agent(
            id=_AGENT_ID,
            name="Acme Display",
            tenant_id=tenant_id,
            metadata={"daimon_name": "acme-config", "daimon_account": str(uuid.uuid4())},
        ),
        ma_agent(id="agent_helper", name="helper", tenant_id=tenant_id),
    )
    return dataclasses.replace(runtime, client=build_fake_anthropic(router.dispatch))


def _helper_turn(tenant_id: uuid.UUID, *, is_admin: bool = False) -> AuthIdentity:
    """A chat turn run by ``helper``: unpinned, so it may publish."""
    return dataclasses.replace(
        _member(tenant_id, is_admin=is_admin),
        chat_agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agent_helper"),
    )


def _ok_publish(captured: dict[str, object]):  # type: ignore[no-untyped-def]
    async def fake(**kwargs: object) -> PublishResult:
        captured.update(kwargs)
        authorize_source = kwargs["authorize_source"]
        assert authorize_source is not None
        source = await find_agent_by_daimon_tag(
            kwargs["anthropic"],  # type: ignore[arg-type]
            tenant_id=kwargs["tenant_id"],  # type: ignore[arg-type]
            name="acme-config",
        )
        assert source is not None
        await authorize_source(source)  # type: ignore[operator]
        return PublishResult(upload_url="u", links={})

    return fake


async def test_a_member_cannot_publish_a_pinned_agents_reader_from_outside(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    runtime.settings.mcp.jwt_secret = SecretStr("s" * 32)
    runtime = _with_helper(runtime, tenant_id)
    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", _ok_publish({}))

    with pytest.raises(ToolError, match="pinned this agent to its own channels"):
        await _publish_report_impl(
            runtime,
            tenant_id=tenant_id,
            account_id=uuid.uuid4(),
            slug="q1",
            title="Q1",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="1",
            agent="acme-config",
            auth=_helper_turn(tenant_id),
        )


async def test_publishing_from_inside_the_pin_or_as_admin_is_allowed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    runtime.settings.mcp.jwt_secret = SecretStr("s" * 32)
    runtime = _with_helper(runtime, tenant_id)
    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", _ok_publish({}))
    account = await make_account(db_session, tenant=await get_tenant(db_session, tenant_id))
    await db_session.commit()
    member = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )
    async with db_session_factory.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant_id,
            account_id=account.id,
            platform="discord",
            parent_channel_id="C_ACME",
            thread_id="T1",
            responder_ma_agent_id=_AGENT_ID,
            responder_name="acme-config",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )

    out = await _publish_report_impl(
        runtime,
        tenant_id=tenant_id,
        account_id=account.id,
        slug="q1",
        title="Q1",
        recipients=[Recipient(name="Ada", label="ada")],
        cap_usd="1",
        agent="acme-config",
        auth=member,
        origin_context_id=str(origin.id),
    )
    assert out == {"upload_url": "u", "links": {}}

    admin_out = await _publish_report_impl(
        runtime,
        tenant_id=tenant_id,
        account_id=uuid.uuid4(),
        slug="q2",
        title="Q2",
        recipients=[Recipient(name="Ada", label="ada")],
        cap_usd="1",
        agent="acme-config",
        auth=_helper_turn(tenant_id, is_admin=True),
    )
    assert admin_out == {"upload_url": "u", "links": {}}


async def test_a_chat_turn_whose_agent_is_gone_publishes_nothing_under_a_pin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It may have been a pinned agent, so it fails closed, admins included."""
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    runtime.settings.mcp.jwt_secret = SecretStr("s" * 32)
    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", _ok_publish({}))

    with pytest.raises(ToolError, match="agent could not be found"):
        await _publish_report_impl(
            runtime,
            tenant_id=tenant_id,
            account_id=uuid.uuid4(),
            slug="q1",
            title="Q1",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="1",
            agent="acme-config",
            auth=_member(tenant_id, is_admin=True),
        )

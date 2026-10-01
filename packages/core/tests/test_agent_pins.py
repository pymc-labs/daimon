"""The pinned-agent write rule as the private forms' submit paths apply it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import (
    PIN_WRITE_REFUSAL,
    POLICY_UNREADABLE_REFUSAL,
    is_pin_administered,
    pin_write_refused,
    request_pin_refusal,
)
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_platform_role_ids, set_role
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import CredentialRequestRow, Role
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ACME = "C_ACME"
_CLIENT_B = "C_CLIENTB"


def _row(
    *, tenant_id: uuid.UUID, account_id: uuid.UUID, channel: str, target_name: str | None
) -> CredentialRequestRow:
    now = datetime.now(UTC)
    return CredentialRequestRow(
        token="tok",
        kind="env",
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        account_id=account_id,
        target="NOTES_TOKEN",
        mcp_server_url=None,
        requester_platform_user_id="U1",
        channel_id=channel,
        platform="slack",
        parent_channel_id=channel,
        origin_thread_id="1700000000.000001",
        idempotency_key=uuid.uuid4(),
        target_name=target_name,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        used_at=None,
    )


async def _setup(
    db_session: AsyncSession, *, pins: dict[str, tuple[str, ...]] | None, role: Role = Role.USER
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await set_role(db_session, account.id, role)
    if pins is not None:
        await set_access_policy(
            db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(agent_channel_pins=pins)
        )
    await db_session.flush()
    return tenant.id, account.id


@pytest.mark.parametrize(
    ("pinned_name", "row_name"),
    [
        ("acme-config", "Acme Display"),
        ("Acme Display", "Acme Display"),
        ("acme-config", "old-name"),
        ("acme-config", None),
    ],
    ids=["pin-on-config-name", "pin-on-display-name", "renamed-since-mint", "legacy-no-name"],
)
async def test_submit_refuses_a_pinned_target_from_outside_by_its_current_names(
    db_session: AsyncSession, pinned_name: str, row_name: str | None
) -> None:
    """The target is the agent as it is now, by every name a pin can be keyed by,
    not whatever name the card saved at mint."""
    tenant_id, account_id = await _setup(db_session, pins={pinned_name: (_ACME,)})
    agent = ma_agent(
        id="ag_acme",
        name="Acme Display",
        tenant_id=tenant_id,
        metadata={"daimon_name": "acme-config"},
    )
    row = _row(tenant_id=tenant_id, account_id=account_id, channel=_CLIENT_B, target_name=row_name)

    assert await request_pin_refusal(db_session, row=row, agent=agent) == PIN_WRITE_REFUSAL
    inside = row.model_copy(update={"parent_channel_id": _ACME, "channel_id": _ACME})
    assert await request_pin_refusal(db_session, row=inside, agent=agent) is None


async def test_a_pin_added_after_the_card_was_posted_holds_at_submit(
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _setup(db_session, pins=None)
    agent = ma_agent(id="ag_acme", name="acme-project", tenant_id=tenant_id)
    row = _row(tenant_id=tenant_id, account_id=account_id, channel=_CLIENT_B, target_name=None)
    assert await request_pin_refusal(db_session, row=row, agent=agent) is None

    await set_access_policy(
        db_session,
        tenant_id=tenant_id,
        policy=TenantAccessPolicy(agent_channel_pins={"acme-project": (_ACME,)}),
    )
    assert await request_pin_refusal(db_session, row=row, agent=agent) == PIN_WRITE_REFUSAL


async def test_an_unresolvable_target_fails_closed_only_under_a_pin(
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _setup(db_session, pins={"acme-project": (_ACME,)})
    row = _row(tenant_id=tenant_id, account_id=account_id, channel=_ACME, target_name=None)
    assert await request_pin_refusal(db_session, row=row, agent=None) == PIN_WRITE_REFUSAL

    open_tenant, open_account = await _setup(db_session, pins=None)
    open_row = _row(tenant_id=open_tenant, account_id=open_account, channel=_ACME, target_name=None)
    assert await request_pin_refusal(db_session, row=open_row, agent=None) is None


async def test_an_admins_request_from_outside_the_pin_is_applied(
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _setup(
        db_session, pins={"acme-project": (_ACME,)}, role=Role.ADMIN
    )
    agent = ma_agent(id="ag_acme", name="acme-project", tenant_id=tenant_id)
    row = _row(tenant_id=tenant_id, account_id=account_id, channel=_CLIENT_B, target_name=None)
    assert await request_pin_refusal(db_session, row=row, agent=agent) is None


async def test_a_dm_origin_is_outside_every_pin(db_session: AsyncSession) -> None:
    tenant_id, account_id = await _setup(db_session, pins={"acme-project": (_ACME,)})
    agent = ma_agent(id="ag_acme", name="acme-project", tenant_id=tenant_id)
    row = _row(tenant_id=tenant_id, account_id=account_id, channel=_ACME, target_name=None)
    dm = row.model_copy(update={"origin_thread_id": "dm:3f1c0a52-0000-4000-8000-000000000000"})
    assert await request_pin_refusal(db_session, row=dm, agent=agent) == PIN_WRITE_REFUSAL


async def test_an_unreadable_policy_refuses(db_session: AsyncSession) -> None:
    tenant_id, account_id = await _setup(db_session, pins=None)
    await db_session.execute(
        text("INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, 'null'::jsonb)"),
        {"t": tenant_id},
    )
    agent = ma_agent(id="ag_acme", name="acme-project", tenant_id=tenant_id)
    row = _row(tenant_id=tenant_id, account_id=account_id, channel=_ACME, target_name=None)
    assert await request_pin_refusal(db_session, row=row, agent=agent) == POLICY_UNREADABLE_REFUSAL


def test_only_an_admin_of_every_pinned_channel_writes_from_outside_the_pin() -> None:
    policy = TenantAccessPolicy(
        agent_channel_pins={"acme": (_ACME, "C_OPS"), "nowhere": (), "solo": (_ACME,)}
    )

    def refused(name: str, administered: set[str]) -> bool:
        return pin_write_refused(
            policy,
            is_admin=False,
            agent_names=(name, None),
            parent_channel_id=_CLIENT_B,
            thread_id=None,
            administered_channel_ids=administered,
        )

    assert refused("acme", {_ACME}), "the admin of one pinned channel of two is refused"
    assert not refused("acme", {_ACME, "C_OPS", _CLIENT_B}), "the admin of both is let through"
    assert not refused("solo", {_ACME}), "a single-channel pin needs only that channel"
    assert refused("nowhere", {_ACME}), "a pin to no channel stays with server admins"
    assert refused("solo", set()), "no grant, no exemption"
    assert not is_pin_administered(
        policy, agent_names=("unpinned",), administered_channel_ids={_ACME}
    ), "an unpinned agent is not administered through a pin"
    assert pin_write_refused(
        policy,
        is_admin=False,
        agent_names=None,
        parent_channel_id=_ACME,
        thread_id=None,
        administered_channel_ids={_ACME, "C_OPS"},
    ), "an unresolvable target still fails closed for a channel admin"


async def test_a_channel_admins_request_from_outside_a_pin_they_run_is_applied(
    db_session: AsyncSession,
) -> None:
    """The requester's stored grants count, by user id and by the roles of their last turn."""
    tenant_id, account_id = await _setup(db_session, pins={"acme-project": (_ACME,)})
    agent = ma_agent(id="ag_acme", name="acme-project", tenant_id=tenant_id)
    row = _row(tenant_id=tenant_id, account_id=account_id, channel=_CLIENT_B, target_name=None)
    assert await request_pin_refusal(db_session, row=row, agent=agent) == PIN_WRITE_REFUSAL

    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        channel_id=_ACME,
        role_ids=[],
        user_ids=["U1"],
        actor_account_id=None,
    )
    assert await request_pin_refusal(db_session, row=row, agent=agent) is None

    discord = row.model_copy(update={"platform": "discord", "requester_platform_user_id": "9"})
    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id=_ACME,
        role_ids=["r1"],
        user_ids=[],
        actor_account_id=None,
    )
    assert await request_pin_refusal(db_session, row=discord, agent=agent) == PIN_WRITE_REFUSAL
    await set_platform_role_ids(db_session, account_id, ["r1"])
    assert await request_pin_refusal(db_session, row=discord, agent=agent) is None, (
        "a role held at the requester's last turn grants it"
    )

"""A person with no internal role is an outsider on the real Discord entry points.

Cold DM, existing DM reply, DM timer and channel timer each compute the requester's
standing from the event roles, then the member mirror, then one member fetch. Ids are
fictional. Mode `on` is faked because the registry has no `on` value yet.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import timer_delivery
from daimon.adapters.discord.commands import direct_messages
from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
from daimon.core import direct_messages as core_dm
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.config import DirectMessagePolicy
from daimon.core.membership import Membership
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.workspace_members import upsert_member
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot
from .test_continuation_dispatch_entry import _make_runtime  # pyright: ignore[reportPrivateUsage]

INTERNAL = "9001"
OTHER_ROLE = "9002"
OWNER = "555"


@pytest.fixture(autouse=True)
def _mode_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def on(*_a: object, **_k: object) -> str:
        return "on"

    monkeypatch.setattr("daimon.core.channel_audience.audience_mode", on)


async def _tenant(factory: async_sessionmaker[AsyncSession], workspace: str) -> uuid.UUID:
    async with factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=workspace)
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await set_access_policy(
            session, tenant_id=tenant.id, policy=TenantAccessPolicy(internal_role_ids=(INTERNAL,))
        )
    return tenant.id


async def _mirror(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, roles: list[str]
):
    async with factory.begin() as session:
        await upsert_member(
            session,
            tenant_id=tenant_id,
            platform_user_id=OWNER,
            is_admin=False,
            now=datetime.now(UTC),
            role_ids=roles,
        )


def _fetch(*roles: str) -> AsyncMock:
    resolver = AsyncMock()
    resolver.resolve.return_value = Membership(is_member=True, role_ids=roles)
    return resolver


def _row(tenant_id: uuid.UUID) -> Any:
    return SimpleNamespace(
        tenant_id=tenant_id,
        requester_external_user_id=OWNER,
        parent_channel_id="dm",
        thread_id="dm:x",
    )


# DM timer: no event roles, so the member mirror, then one fetch --------------------------


@pytest.mark.parametrize(
    ("mirror", "fetch_roles", "expected"),
    [
        ([OTHER_ROLE], (INTERNAL,), RequesterOrg.OUTSIDER),  # role removed; mirror is current
        (None, (INTERNAL,), RequesterOrg.HOST),  # mirror cleared; fetch keeps an internal person
        (None, (OTHER_ROLE,), RequesterOrg.OUTSIDER),  # mirror cleared; fetch finds no role
        ([INTERNAL], (), RequesterOrg.HOST),  # the mirror answers without a fetch
    ],
)
async def test_a_dm_timer_follows_the_mirror_then_one_fetch(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mirror: list[str] | None,
    fetch_roles: tuple[str, ...],
    expected: RequesterOrg,
) -> None:
    tenant_id = await _tenant(db_session_factory, "720000901")
    if mirror is not None:
        await _mirror(db_session_factory, tenant_id, mirror)
    bot = make_bot(_make_runtime(db_session_factory))
    bot.membership_resolver = _fetch(*fetch_roles)  # pyright: ignore[reportAttributeAccessIssue]
    run = AsyncMock(return_value=SimpleNamespace(text="done"))
    monkeypatch.setattr(core_dm, "run_dm_timer", run)

    async def deliver(*_a: object, run_turn: Any, **_k: object) -> int:
        await run_turn()
        return 1

    monkeypatch.setattr(timer_delivery, "deliver_dm_timer", deliver)

    await bot._run_dm_timer(_row(tenant_id), "seed")  # pyright: ignore[reportPrivateUsage]

    assert run.await_args.kwargs["requester_org"] is expected
    resolver: AsyncMock = bot.membership_resolver  # pyright: ignore[reportAssignmentType]
    assert resolver.resolve.await_count == (1 if mirror is None else 0), "one fetch, no mirror"


# Channel timer: the member the access check found carries the roles ----------------------


@pytest.mark.parametrize(
    ("roles", "expected"),
    [([OTHER_ROLE], RequesterOrg.OUTSIDER), ([INTERNAL], RequesterOrg.HOST)],
)
async def test_a_channel_timer_reads_the_roles_of_the_member_it_found(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    roles: list[str],
    expected: RequesterOrg,
) -> None:
    tenant_id = await _tenant(db_session_factory, "720000902")
    bot = make_bot(_make_runtime(db_session_factory))
    bot.membership_resolver = _fetch()  # pyright: ignore[reportAttributeAccessIssue]
    run = AsyncMock(return_value=SimpleNamespace(text="done"))
    monkeypatch.setattr(core_dm, "run_channel_timer", run)
    member = MagicMock()
    member.roles = [SimpleNamespace(id=int(r), is_default=lambda: False) for r in roles]
    member.guild.owner_id = 1
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    member.id = int(OWNER)

    async def deliver(row: Any, *, check_access: Any, run_turn: Any, **_k: object) -> int:
        await run_turn(SimpleNamespace(member=member, category_id=None))
        return 1

    monkeypatch.setattr(timer_delivery, "deliver_channel_timer", deliver)

    await bot._run_channel_timer(_row(tenant_id), "seed")  # pyright: ignore[reportPrivateUsage]

    assert run.await_args.kwargs["requester_org"] is expected


# Cold DM and existing DM: the subject carries the roles from one fresh fetch -------------


def _cog(factory: async_sessionmaker[AsyncSession], roles: tuple[str, ...]) -> DirectMessageCog:
    bot = MagicMock()
    bot.runtime.sessionmaker = factory
    bot.runtime.turn_deps.membership_resolver = _fetch(*roles)
    bot.allows_tenant.return_value = True
    cog = DirectMessageCog(bot)
    subject = Subject(
        is_admin=False,
        platform_user_id=OWNER,
        verified_membership=True,
        role_ids=roles,
    )
    cog._subject = AsyncMock(return_value=subject)  # pyright: ignore[reportPrivateUsage]
    return cog


def _allow_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(direct_messages, "env_dm_policy", lambda *_a, **_k: DirectMessagePolicy())


@pytest.mark.parametrize(
    ("roles", "admitted"), [((OTHER_ROLE,), False), ((INTERNAL,), True), ((), False)]
)
async def test_a_cold_dm_from_a_person_without_an_internal_role_is_not_admitted(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    roles: tuple[str, ...],
    admitted: bool,
) -> None:
    _allow_all(monkeypatch)
    tenant_id = await _tenant(db_session_factory, "720000903")
    async with db_session_factory() as session:
        from daimon.core.stores.tenants import get_tenant

        tenant = await get_tenant(session, tenant_id)
    assert tenant is not None
    monkeypatch.setattr(direct_messages, "list_routable_dm_agents", AsyncMock(return_value=[]))
    decision = SimpleNamespace(route=SimpleNamespace(agent="helper"), may_use=lambda _a: True)
    monkeypatch.setattr(direct_messages, "decide_dm", AsyncMock(return_value=decision))
    monkeypatch.setattr(direct_messages, "DmUnchecked", type("U", (), {}))
    monkeypatch.setattr(direct_messages, "DmRefused", type("R", (), {}))
    monkeypatch.setattr(direct_messages, "DmChoose", type("C", (), {}))

    async def cold(_deps: object, *, requester_org: RequesterOrg, **_k: object) -> object:
        if requester_org is RequesterOrg.OUTSIDER:
            raise AdmissionDenied(reason="client_requester")
        return "admission"

    monkeypatch.setattr(direct_messages, "admit_cold_dm", cold)
    message = MagicMock(spec=discord.Message)
    message.author = SimpleNamespace(id=int(OWNER), bot=False)
    message.channel = MagicMock(spec=discord.DMChannel)
    message.channel.id = 1234
    message.channel.send = AsyncMock()

    found = await _cog(db_session_factory, roles)._eligible(  # pyright: ignore[reportPrivateUsage]
        tenant, message
    )

    assert (found.admission is not None) is admitted
    message.channel.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("roles", "expected"),
    [((OTHER_ROLE,), RequesterOrg.OUTSIDER), ((INTERNAL,), RequesterOrg.HOST)],
)
async def test_an_existing_dm_reply_reads_the_roles_of_its_fresh_fetch(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    roles: tuple[str, ...],
    expected: RequesterOrg,
) -> None:
    _allow_all(monkeypatch)
    tenant_id = await _tenant(db_session_factory, "720000904")
    cog = _cog(db_session_factory, roles)
    ticket = MagicMock()
    ticket.wait = AsyncMock(return_value="started")
    cog.bot.turn_queue.admit.return_value = ticket
    cog.bot.runtime.settings.discord = None
    reply = AsyncMock(return_value=None)
    monkeypatch.setattr(direct_messages, "reply_to_dm_result", reply)
    monkeypatch.setattr(direct_messages, "compose_queued_content", lambda _m: "hello")
    monkeypatch.setattr(direct_messages, "prepare_attachments", AsyncMock(return_value=("", [])))
    monkeypatch.setattr(direct_messages, "input_context", AsyncMock(return_value=""))
    monkeypatch.setattr(direct_messages, "tenant_ready", lambda *_a, **_k: True)
    message = MagicMock(spec=discord.Message)
    message.id = 1
    message.author = SimpleNamespace(id=int(OWNER), bot=False)
    message.channel = MagicMock(spec=discord.DMChannel)
    message.channel.id = 1234
    message.channel.send = AsyncMock()
    message.attachments = []
    conversation = SimpleNamespace(tenant_id=tenant_id, scope_id="dm:x")

    await cog._turn(message, conversation, (message,))  # pyright: ignore[reportPrivateUsage]

    assert reply.await_args.kwargs["requester_org"] is expected

"""Gateway evidence is timestamped, with immediate member/guild invalidation."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.membership import GatewayMembershipResolver, member_facts
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.membership import Membership


@dataclass
class _Role:
    id: int
    default: bool = False
    name: str = ""

    def is_default(self) -> bool:
        return self.default


def _member():  # type: ignore[no-untyped-def]
    member = MagicMock(spec=discord.Member)
    member.id = 2
    member.guild.id = 1
    member.guild.owner_id = 3
    member.guild.unavailable = False
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    everyone = _Role(id=1, default=True)
    role = _Role(id=123)
    member.roles = [everyone, role]
    return member


@pytest.mark.parametrize("cached_guild", [True, False])
async def test_gateway_live_fetch_excludes_everyone_and_ignores_sdk_member_cache(
    cached_guild: bool,
) -> None:
    member = _member()
    guild = member.guild
    guild.fetch_member = AsyncMock(return_value=member)
    guild.get_member = MagicMock(side_effect=AssertionError("untimestamped cache is not proof"))
    client = MagicMock(spec=discord.Client)
    client.get_guild.return_value = guild if cached_guild else None
    client.fetch_guild = AsyncMock(return_value=guild)
    resolver = GatewayMembershipResolver(client)
    tenant = derive_tenant_uuid(platform="discord", workspace_id="1")
    assert (await resolver.resolve(tenant, "1", "2")).role_ids == ("123",)
    member.roles[1].name = "renamed core"
    assert member_facts(member).role_ids == ("123",)
    await resolver.resolve(tenant, "1", "2", require_fresh=True)
    assert guild.fetch_member.await_count == 2


async def test_member_update_remove_and_guild_unavailability_revoke_cached_grants() -> None:
    member = _member()
    client = MagicMock(spec=discord.Client)
    client.get_guild.return_value = None
    client.fetch_guild = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404, reason="Unknown Guild"), "unknown guild")
    )
    resolver = GatewayMembershipResolver(client)
    tenant = derive_tenant_uuid(platform="discord", workspace_id="1")
    host = SimpleNamespace(
        membership_resolver=resolver,
        _allows_guild=lambda _: True,
        user=None,
        _member_sync=SimpleNamespace(joined=AsyncMock(), left=AsyncMock()),
    )
    resolver.observe(tenant, "1", "2", Membership(is_member=True, role_ids=("123",)))
    member.roles = []
    await DaimonBot.on_member_update(host, _member(), member)  # type: ignore[arg-type]
    assert (await resolver.resolve(tenant, "1", "2")).role_ids == ()
    await DaimonBot.on_raw_member_remove(
        host, SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2))
    )  # type: ignore[arg-type]
    assert (await resolver.resolve(tenant, "1", "2")).is_member is None
    resolver.observe(tenant, "1", "2", Membership(is_member=True, role_ids=("123",)))
    await DaimonBot.on_guild_unavailable(host, SimpleNamespace(id=1))  # type: ignore[arg-type]
    assert (await resolver.resolve(tenant, "1", "2")).is_member is None


async def test_queued_member_snapshot_cannot_restore_a_removed_role(
    db_session, db_session_factory, monkeypatch
):  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.membership import CachedMembershipResolver
    from daimon.core.stores.access_policy import set_access_policy

    from .conftest import make_tenant
    from .harness import make_bot
    from .test_orchestration import (
        _make_channel_message,
        _make_runtime,
        _setup_workspace_and_config,
    )

    tenant = await make_tenant(db_session, workspace_id="123456")
    await _setup_workspace_and_config(db_session, tenant.id)
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(invoker_role_ids=("123",))
    )
    await db_session.commit()
    bot = make_bot(_make_runtime(tenant.id, db_session_factory))
    resolver = CachedMembershipResolver(AsyncMock(return_value=Membership(is_member=True)))
    bot.membership_resolver = resolver
    bot.runtime = replace(
        bot.runtime, turn_deps=replace(bot.runtime.turn_deps, membership_resolver=resolver)
    )
    message = _make_channel_message(author_id=2)
    author = _member()
    author.guild = message.guild
    author.bot = False
    message.author = author
    # The queued message retains the old role, but the member update revoked it.
    resolver.observe(tenant.id, "123456", "2", Membership(is_member=True))
    config = AsyncMock(
        side_effect=AssertionError("revocation must refuse before config or model I/O")
    )
    monkeypatch.setattr("daimon.core.turn.admission.resolve_config", config)
    await bot._orchestrate(message, "123456", tenant.id)  # pyright: ignore[reportPrivateUsage]
    config.assert_not_awaited()
    message.channel.send.assert_awaited_once()
    assert (
        "can't accept your request here"
        in message.channel.send.await_args.kwargs["embed"].description
    )

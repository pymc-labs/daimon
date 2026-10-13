"""`daimon spaces` against the real database: the audience check, then each change."""

from __future__ import annotations

from io import StringIO
from typing import cast

import pytest
import typer
from daimon.adapters.cli.commands.spaces import (
    spaces_add,
    spaces_create,
    spaces_delete,
    spaces_list,
    spaces_remove,
)
from daimon.adapters.cli.commands.tenants import tenants_access_policy_set
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.permissions import channel_rule
from daimon.core.space_rules import SpaceRefused
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=200)


def _out(console: Console) -> str:
    return cast(StringIO, console.file).getvalue()


async def test_spaces_need_confirming_then_create_add_list_remove_delete(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await provision_tenant(db_session_factory, platform="slack", workspace_id="T1")
    rt = build_cli_runtime(db_session_factory)
    console = _console()
    args = {"rt": rt, "console": console, "platform": "slack", "workspace_id": "T1"}

    with pytest.raises(typer.Exit):
        await spaces_create(**args, name="client", channel_ids=["C1", "C2"])
    assert "C1: 0 external account(s)" in _out(console), "what is known is shown"
    assert "can't see who reads a channel" in _out(console), "and why it must be confirmed"
    async with db_session_factory() as session:
        assert (await load_access_policy(session, tenant_id=tenant.tenant_id)).spaces == {}, (
            "nothing was created unconfirmed"
        )

    await spaces_create(**args, name="client", channel_ids=["C1", "C2"], confirm=True)
    assert "C1: readers any, writers any -> readers own, writers own" in _out(console), (
        "each member's rule is shown before and after"
    )
    await spaces_add(**args, name="client", channel_id="C3", confirm=True)
    await spaces_list(**args)
    assert "client: readers own, writers own; C1, C2, C3" in _out(console), "listed"
    await spaces_remove(**args, name="client", channel_id="C3")
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.tenant_id)
    (space,) = policy.spaces.values()
    assert space.channel_ids == {"C1", "C2"}, "C3 left"
    assert channel_rule(policy, "C3") == ChannelRule(readers="own", writers="own"), "contained"

    with pytest.raises(typer.BadParameter, match="delete the Spaces first"):
        await tenants_access_policy_set(
            rt=rt, console=console, platform="slack", external_id="T1", clear=True
        )
    await spaces_delete(**args, name="client")
    assert "Space client deleted; C1, C2 keep its rule." in _out(console), "deleted"


async def test_spaces_add_refuses_to_loosen_a_rule_unless_asked(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A channel taking no replies keeps them off unless the operator asks to loosen it."""
    tenant = await provision_tenant(db_session_factory, platform="slack", workspace_id="T1")
    rt = build_cli_runtime(db_session_factory)
    console = _console()
    args = {"rt": rt, "console": console, "platform": "slack", "workspace_id": "T1"}
    async with db_session_factory() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.tenant_id,
            policy=TenantAccessPolicy(channel_rules={"C2": ChannelRule(writers="none")}),
        )
        await session.commit()
    await spaces_create(**args, name="client", channel_ids=["C1"], readers="inside")

    with pytest.raises(SpaceRefused, match="writers none to any"):
        await spaces_add(**args, name="client", channel_id="C2", confirm=True)
    await spaces_add(**args, name="client", channel_id="C2", confirm=True, loosen_rules=True)
    assert "C2: readers any, writers none -> readers inside, writers any" in _out(console), (
        "the loosened rule is shown"
    )

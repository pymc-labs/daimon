"""Channel isolation: who is local to an isolated channel, and which bindings break it."""

from __future__ import annotations

import uuid
from typing import Literal

from daimon.core.access_policy import TenantAccessPolicy, is_isolated
from daimon.core.channel_isolation import (
    NO_ISOLATION,
    ChannelIsolation,
    build_channel_isolation,
    load_channel_isolation,
)
from daimon.core.scope import ChannelConfigRow, ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import (
    create_binding,
    list_handoff_parent_channel_ids,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT = DeploymentDefault(agent_name="daimon")
TENANT = uuid.uuid4()


def _channel(channel_id: str, agent: str) -> ChannelConfigRow:
    return ChannelConfigRow(tenant_id=TENANT, channel_id=channel_id, agent_name=agent)


def _isolation(*, threads: dict[str, list[str]] | None = None) -> ChannelIsolation:
    return build_channel_isolation(
        {"c1"},
        tenant=None,
        channels=[_channel("c1", "local"), _channel("c2", "shared"), _channel("c1b", "shared")],
        default=DEFAULT,
        thread_parent_channel_ids=threads or {},
    )


def test_nothing_isolated_is_inert() -> None:
    isolation = build_channel_isolation(
        (),
        tenant=None,
        channels=[_channel("c1", "local")],
        default=DEFAULT,
        thread_parent_channel_ids={},
    )
    assert isolation is NO_ISOLATION and not isolation.is_active, "no ids means no isolation"
    assert isolation.is_visible("local", inside_channel_id=None), "every agent stays visible"
    assert isolation.binding_refusal("local", channel_id="c2") is None, "every binding allowed"


def test_only_agents_answering_solely_in_the_channel_are_local() -> None:
    isolation = _isolation(threads={"shared": ["c1"], "helper": ["c1"], "roamer": ["c1", "c3"]})
    assert isolation.channel_of("local") == "c1", "the channel default answering only in c1"
    assert isolation.channel_of("helper") == "c1", "a thread under c1 is inside c1"
    assert isolation.channel_of("shared") is None, "answers in c2 too"
    assert isolation.channel_of("roamer") is None, "a thread elsewhere makes it shared"
    assert isolation.channel_of("daimon") is None, "the tenant-wide default is never local"


def test_visibility_splits_inside_from_outside() -> None:
    isolation = _isolation()
    assert isolation.is_visible("local", inside_channel_id="c1"), "local agent seen inside"
    assert not isolation.is_visible("local", inside_channel_id=None), "hidden outside"
    assert not isolation.is_visible("shared", inside_channel_id="c1"), "inside sees only local"
    assert isolation.is_visible("shared", inside_channel_id=None), "outside sees shared"


def test_binding_refusals_keep_local_agents_in_and_shared_agents_out() -> None:
    isolation = _isolation()
    assert isolation.binding_refusal("local", channel_id="c2") == "agent_confined"
    assert isolation.binding_refusal("local", channel_id=None) == "agent_confined", (
        "a local agent can't become the tenant default"
    )
    assert isolation.binding_refusal("local", channel_id="c1") is None, "rebinding in c1 is fine"
    assert isolation.binding_refusal("shared", channel_id="c1") == "channel_needs_own_agent"
    assert isolation.binding_refusal("fresh", channel_id="c1") is None, (
        "an agent answering nowhere becomes c1's own"
    )
    assert (
        isolation.binding_refusal("fresh", channel_id="c1", is_daimon_managed=True)
        == "channel_needs_own_agent"
    ), "a built-in agent never becomes local"
    assert isolation.binding_refusal("shared", channel_id="c2") is None, "outside is untouched"
    assert isolation.clear_refusal(channel_id="c1") == "channel_isolated"
    assert isolation.clear_refusal(channel_id="c2") is None


def test_isolated_location_counts_threads_under_the_channel() -> None:
    isolation = _isolation()
    assert isolation.isolated_channel("t1", "c1") == "c1", "a thread under c1"
    assert isolation.isolated_channel("c2") is None
    policy = TenantAccessPolicy(isolated_channel_ids=("c1",))
    assert is_isolated(policy, channel_id="t1", parent_channel_id="c1")
    assert not is_isolated(policy, channel_id="c2"), "other channels stay open"
    assert not is_isolated(TenantAccessPolicy(), channel_id="c1"), "default policy isolates none"


async def test_loader_reads_the_cascade_and_handoff_threads(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    assert (
        await load_channel_isolation(db_session, tenant_id=tenant.id, default=DEFAULT)
        is NO_ISOLATION
    ), "no policy row means no isolation"
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="local",
        mode="agent",
    )
    bindings: list[tuple[Literal["setup", "handoff"], str, str]] = [
        ("handoff", "t1", "helper"),
        ("setup", "t2", "daimon"),
    ]
    for kind, thread, name in bindings:
        await create_binding(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="c1",
            thread_id=thread,
            responder_ma_agent_id=f"agent_{name}",
            responder_name=name,
            kind=kind,
        )
    assert await list_handoff_parent_channel_ids(db_session, tenant_id=tenant.id) == {
        "helper": ["c1"]
    }, "setup conversations route nobody"
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(isolated_channel_ids=("c1",))
    )
    isolation = await load_channel_isolation(db_session, tenant_id=tenant.id, default=DEFAULT)
    assert isolation.agent_channel_ids == {"local": "c1", "helper": "c1"}, (
        "the channel default and the handed-over agent are local; the setup responder is not"
    )

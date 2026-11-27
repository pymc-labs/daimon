"""A home channel's conversations and its own agent stay in the channel, admins included.

The channel is kept to its own agent (readers own, writers own) and the agent runs only
there. A live `Membership.is_admin` is what the Discord resolver gives an owner, an
Administrator or a Manage Server holder: the hub cannot tell them apart, so none of them
reads the channel's conversations or runs its agent from outside it.
"""

from __future__ import annotations

import uuid

import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentRef,
    SessionFacts,
    Surface,
    authorize,
    build_subject,
    build_turn_place,
)
from daimon.core.membership import Membership
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role

from .test_hub_admin_seals import (  # pyright: ignore[reportPrivateUsage]  # noqa: F401
    _TOPIC,
    _Hub,
    hub,
)

_HOME = "c-home"
_POLICY = TenantAccessPolicy(
    channel_rules={_HOME: ChannelRule(readers="own", writers="own")},
    agent_rules={"acme-project": AgentRule(runs_in=(_HOME,))},
)


async def _home_policy(hub: _Hub) -> None:  # noqa: F811
    await set_access_policy(hub.db, tenant_id=hub.tenant_id, policy=_POLICY)
    await hub.db.commit()


async def _caller(hub: _Hub, *, live_admin: bool) -> None:  # noqa: F811
    await set_role(hub.db, hub.account_id, Role.USER)
    await hub.db.commit()

    async def fetch(tenant_id: uuid.UUID, guild_id: str, user_id: str) -> Membership:
        return Membership(is_member=True, is_admin=live_admin)

    hub.membership_resolver._fetch = fetch  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    hub.live_admin = live_admin


@pytest.mark.parametrize("live_admin", [True, False], ids=["admin_or_manage_server", "member"])
async def test_no_hub_caller_reaches_a_home_channel(hub: _Hub, live_admin: bool) -> None:  # noqa: F811
    await _home_policy(hub)
    await _caller(hub, live_admin=live_admin)
    hub.add_session("ses_mine_home", daimon_channel=_HOME, daimon_thread="t1", daimon_sealed=_HOME)
    hub.add_session(
        "ses_theirs_home", account=uuid.uuid4(), daimon_channel=_HOME, daimon_thread="t2"
    )

    for tool, args in (
        ("list_my_sessions", {}),
        ("list_events", {"handle": "ses_theirs_home"}),
        ("get_session", {"handle": "ses_theirs_home"}),
        ("await_turn", {"handle": "ses_theirs_home"}),
        ("ask", {"message": "what did the channel decide?"}),
    ):
        result = await hub.call(tool, **args)
        assert result.get("isError") and _TOPIC not in str(result), (tool, result)
    assert hub.sent == []


_ADMIN = build_subject(is_admin=True, platform_user_id="u-admin", verified_membership=True)
_MEMBER = build_subject(is_admin=False, platform_user_id="u-member", verified_membership=True)
_HOME_AGENT = AgentRef.of("acme-project")


@pytest.mark.parametrize(
    ("subject", "surface", "place", "allowed"),
    [
        (_ADMIN, Surface.DM, None, False),  # was ALLOW: admin agent-rule exemption
        (_ADMIN, Surface.HUB, None, False),
        (_MEMBER, Surface.DM, None, False),
        (_ADMIN, Surface.CHANNEL, build_turn_place(channel_id=_HOME, thread_id="t9"), True),
        (_MEMBER, Surface.CHANNEL, build_turn_place(channel_id=_HOME, thread_id="t9"), True),
    ],
    ids=["admin-dm", "admin-hub", "member-dm", "admin-in-home", "member-in-home"],
)
def test_a_home_agent_runs_only_in_its_home(subject, surface, place, allowed) -> None:  # type: ignore[no-untyped-def]
    decision = authorize(
        _POLICY,
        subject=subject,
        action=Action.RUN_AGENT,
        surface=surface,
        agent=_HOME_AGENT,
        **({"place": place} if place else {}),
    )
    assert bool(decision) is allowed, decision


def test_admin_hub_read_open_only_and_sticky() -> None:
    """An admin reads another member's open-channel session; never a sealed one,
    even after the channel is opened again (seals only grow)."""
    open_policy = TenantAccessPolicy()
    facts = SessionFacts(channel="c-open", owned=False)
    assert authorize(
        open_policy, subject=_ADMIN, action=Action.READ_SESSION, surface=Surface.HUB, session=facts
    )
    sealed = SessionFacts(channel=_HOME, owned=False, seal_ids=frozenset({_HOME}))
    for policy in (_POLICY, open_policy):  # open_policy: an admin opened the channel afterwards
        assert not authorize(
            policy, subject=_ADMIN, action=Action.READ_SESSION, surface=Surface.HUB, session=sealed
        )
    own = SessionFacts(channel=_HOME, owned=True, seal_ids=frozenset({_HOME}))
    assert authorize(
        _POLICY, subject=_ADMIN, action=Action.READ_SESSION, surface=Surface.HUB, session=own
    )


async def test_session_gate_alone_with_a_visible_agent(hub: _Hub) -> None:  # noqa: F811
    """The agent stays listed (readers inside, no home), so only the transcript gate
    decides: an admin reads their own sealed session and an open one of anyone's,
    never another person's sealed one, even after the channel is opened again."""
    inside = TenantAccessPolicy(channel_rules={_HOME: ChannelRule(readers="inside")})
    await set_access_policy(hub.db, tenant_id=hub.tenant_id, policy=inside)
    await hub.db.commit()
    await _caller(hub, live_admin=True)
    other = uuid.uuid4()
    hub.add_session("ses_mine", daimon_channel=_HOME, daimon_thread="t1", daimon_sealed=_HOME)
    hub.add_session(
        "ses_theirs", account=other, daimon_channel=_HOME, daimon_thread="t2", daimon_sealed=_HOME
    )
    hub.add_session("ses_open", account=other, daimon_channel="c-open", daimon_thread="t3")

    listed = str(await hub.call("list_my_sessions"))
    assert "ses_mine" in listed and "ses_open" in listed and "ses_theirs" not in listed, listed
    assert _TOPIC in str(await hub.call("list_events", handle="ses_mine"))
    assert _TOPIC in str(await hub.call("list_events", handle="ses_open"))
    theirs = await hub.call("list_events", handle="ses_theirs")
    assert theirs.get("isError") and "not found" in str(theirs), theirs

    await set_access_policy(hub.db, tenant_id=hub.tenant_id, policy=TenantAccessPolicy())
    await hub.db.commit()
    theirs = await hub.call("list_events", handle="ses_theirs")
    assert theirs.get("isError") and _TOPIC not in str(theirs), theirs

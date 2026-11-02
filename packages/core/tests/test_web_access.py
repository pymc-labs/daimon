"""Who may chat with an agent on the web."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.stores.domain import WebAgentGrantRow, WebAgentWho
from daimon.core.web_access import may_chat

_TENANT = uuid.uuid4()


def _grant(
    *, web: bool = True, who: WebAgentWho = "everyone", groups: tuple[str, ...] = ()
) -> WebAgentGrantRow:
    return WebAgentGrantRow(tenant_id=_TENANT, agent_id="a", web=web, who=who, groups=groups)


def _may(
    grant: WebAgentGrantRow | None,
    *,
    visitor: bool = False,
    admin: bool = False,
    groups: frozenset[str] = frozenset(),
    invited: frozenset[str] = frozenset(),
) -> bool:
    return may_chat(
        "a", grant, is_visitor=visitor, is_admin=admin, groups=groups, invited_agent_ids=invited
    )


def test_no_grant_is_on_for_everyone() -> None:
    assert _may(None), "an agent nobody configured chats with every member"


@pytest.mark.parametrize("admin", [False, True])
def test_web_off_refuses_everyone(admin: bool) -> None:
    assert not _may(_grant(web=False), admin=admin, invited=frozenset({"a"})), "off is off"


def test_visitor_reaches_only_invited_agents() -> None:
    assert _may(None, visitor=True, invited=frozenset({"a"})), "the invite's agent is open"
    assert not _may(None, visitor=True), "any other agent is closed, even one for everyone"


def test_groups_need_a_shared_group() -> None:
    grant = _grant(who="groups", groups=("Sales",))
    assert _may(grant, groups=frozenset({"Sales", "Ops"})), "a member of the group may chat"
    assert not _may(grant, groups=frozenset({"Ops"})), "anyone else may not"
    assert _may(grant, admin=True), "an admin reaches every agent that is on"


def test_invites_only_admits_invited_members() -> None:
    grant = _grant(who="invites")
    assert _may(grant, invited=frozenset({"a"})), "a member invited to the agent may chat"
    assert not _may(grant), "a member without that invite may not"

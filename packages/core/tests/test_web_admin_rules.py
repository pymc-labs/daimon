"""Web admin rules that need no database: limits, the self-suspend guard, one-link revoke."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core import web_admin
from daimon.core import web_roles_rules as rules
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import web_invites, web_people
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.web_admin import AgentAccess, WebAdmin, WebAdminError
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DB = cast(AsyncSession, None)


def _admin(person_id: uuid.UUID | None = None) -> WebAdmin:
    return WebAdmin(
        tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), role="admin", person_id=person_id
    )


async def test_create_invite_when_more_than_fifty_agents_then_refused() -> None:
    agents = {f"a{i}": f"A{i}" for i in range(51)}
    with pytest.raises(WebAdminError) as err:
        await web_admin.create_invite(
            DB,
            _admin(),
            NOW,
            email="v@example.com",
            role="visitor",
            agent_ids=list(agents),
            agents=agents,
            credit_usd=Decimal(0),
            days=7,
            allow_files=False,
        )
    assert err.value.status == 422, "too many agents is refused before anything is written"


async def test_set_agent_access_when_more_than_a_hundred_groups_then_refused() -> None:
    current = AgentAccess(
        agent_id="a1", name="A", web=True, who="everyone", groups=[], fixed_to=None
    )
    with pytest.raises(WebAdminError) as err:
        await web_admin.set_agent_access(
            DB, _admin(), NOW, current, groups=[f"g{i}" for i in range(101)]
        )
    assert err.value.status == 422, "too many groups is refused before anything is written"


async def test_change_person_when_suspending_self_then_refused_by_person_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    me = uuid.uuid4()
    target = SimpleNamespace(id=me, tenant_id=uuid.uuid4(), status="active", account_id=None)

    async def person_in(*_a: Any, **_k: Any) -> Any:
        return target

    async def role_of(*_a: Any, **_k: Any) -> str:
        return "member"

    monkeypatch.setattr(web_admin, "person_in", person_in)
    monkeypatch.setattr(web_admin, "role_of", role_of)
    with pytest.raises(WebAdminError) as err:
        await web_admin.change_person(DB, _admin(me), NOW, me, status="suspended")
    assert err.value.status == 409, "an admin cannot suspend their own web person"


async def test_change_person_when_suspending_another_with_same_account_then_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), status="active")
    updated: list[Any] = []

    async def person_in(*_a: Any, **_k: Any) -> Any:
        return other

    async def role_of(*_a: Any, **_k: Any) -> str:
        return "member"

    async def update_person(*_a: Any, **kw: Any) -> None:
        updated.append(kw)

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(web_admin, "person_in", person_in)
    monkeypatch.setattr(web_admin, "role_of", role_of)
    monkeypatch.setattr(web_people, "update_person", update_person)
    monkeypatch.setattr(web_invites, "revoke_open_links", nothing)
    monkeypatch.setattr(web_admin, "audit", nothing)
    await web_admin.change_person(DB, _admin(uuid.uuid4()), NOW, other.id, status="suspended")
    assert updated and updated[0]["status"] == "suspended", "another person may be suspended"


async def test_revoke_issued_link_when_called_then_only_that_link_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_id = uuid.uuid4()
    seen: list[Any] = []

    async def revoke_link(_db: Any, **kw: Any) -> Any:
        seen.append(kw["link_id"])
        return SimpleNamespace(tenant_id=uuid.uuid4(), person_id=uuid.uuid4())

    async def revoke_open_links(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the person's other links must stay")

    async def audit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(web_invites, "revoke_link", revoke_link)
    monkeypatch.setattr(web_invites, "revoke_open_links", revoke_open_links)
    monkeypatch.setattr(web_admin, "audit", audit)
    await web_admin.revoke_issued_link(DB, _admin(), NOW, link_id)
    assert seen == [link_id], "only the link just issued is revoked"


async def _role_of_account(
    monkeypatch: pytest.MonkeyPatch, person: Any, web_role: str, *, chat_admin: bool
) -> Any:
    async def get_person(*_a: Any, **_k: Any) -> Any:
        return person

    async def role_of(*_a: Any, **_k: Any) -> str:
        return web_role

    async def no_grant(*_a: Any, **_k: Any) -> None:
        return None

    async def no_roles(*_a: Any, **_k: Any) -> dict[uuid.UUID, Any]:
        return {}

    monkeypatch.setattr(people_store, "get_person_for_account", get_person)
    monkeypatch.setattr(rules, "role_of", role_of)
    monkeypatch.setattr(roles_store, "get_workspace_role", no_grant)
    monkeypatch.setattr(accounts_store, "list_roles", no_roles)
    return await rules.role_of_account(
        DB, tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), chat_admin=chat_admin
    )


async def test_role_of_account_when_web_admin_without_chat_admin_then_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = SimpleNamespace(status="active")
    role = await _role_of_account(monkeypatch, person, "admin", chat_admin=False)
    assert role == "admin", "the web person's role decides, not chat standing"


async def test_role_of_account_when_chat_admin_is_web_member_then_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = SimpleNamespace(status="active")
    role = await _role_of_account(monkeypatch, person, "member", chat_admin=True)
    assert role == "member", "chat admin does not lift a web member"


async def test_role_of_account_when_person_suspended_then_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = SimpleNamespace(status="suspended")
    role = await _role_of_account(monkeypatch, person, "admin", chat_admin=True)
    assert role is None, "a suspended person can open nothing"

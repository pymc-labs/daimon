"""Tool results link to the web app only when there is one and the caller may open the page."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._web_url import admin_links, tenant_links
from daimon.adapters.mcp.tools.agents import (  # pyright: ignore[reportPrivateUsage]
    AgentInfo,
    _with_web_url,
)
from daimon.adapters.mcp.tools.channel_environments import (
    ChannelEnvironmentResult,
    _with_channel_link,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.routines import _viewed  # pyright: ignore[reportPrivateUsage]
from daimon.core import web_roles_rules as rules
from daimon.core.config import WebSettings
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores.domain import Role, RoutineRow
from daimon.core.web_admin import WebAdmin
from pydantic import TypeAdapter

from ..harness import make_identity
from .parity_fakes import _Factory  # pyright: ignore[reportPrivateUsage]

_NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)
_TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")
_BASE = f"https://app.example.com/w/{_TENANT}"


def _runtime(web: WebSettings | None) -> McpRuntime:
    return cast(
        McpRuntime,
        SimpleNamespace(client=None, session_factory=_Factory(), settings=SimpleNamespace(web=web)),
    )


@pytest.fixture(autouse=True)
def web_role(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The caller's web role: a chat admin is a web admin unless a test says otherwise."""
    state: dict[str, Any] = {"person": None, "role": None, "grant": None}

    async def person(*_a: Any, **_k: Any) -> Any:
        return state["person"]

    async def role(_db: Any, _person: Any) -> Any:
        return state["role"]

    async def grant(*_a: Any, **_k: Any) -> Any:
        return state["grant"]

    async def no_platform_roles(*_a: Any, **_k: Any) -> dict[uuid.UUID, Any]:
        return {}

    monkeypatch.setattr(people_store, "get_person_for_account", person)
    monkeypatch.setattr(rules, "role_of", role)
    monkeypatch.setattr(roles_store, "get_workspace_role", grant)
    monkeypatch.setattr(accounts_store, "list_roles", no_platform_roles)
    return state


_WEB = WebSettings(public_url="https://app.example.com")  # type: ignore[call-arg]


def _agent() -> AgentInfo:
    return AgentInfo(
        name="stats",
        id="agent_1",
        description=None,
        model="claude-sonnet-5-5",
        created_at=None,
        mcp_servers=None,
        skills=[],
    )


def _routine() -> RoutineRow:
    return RoutineRow.model_validate(
        {
            "id": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"),
            "tenant_id": _TENANT,
            "created_by_user_id": "U1",
            "agent_id": "agent_1",
            "agent_name": "stats",
            "cron_expr": "0 9 * * 1",
            "timezone": "UTC",
            "trigger_message": "go",
            "enabled": True,
            "next_fire_at": None,
            "last_fired_at": None,
            "last_error": None,
            "last_result_tail": None,
            "delivery_payload": None,
            "destination_kind": None,
            "destination_id": None,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )


async def test_no_web_app_means_no_links_and_no_field_in_the_output() -> None:
    auth = make_identity(tenant_id=_TENANT)
    runtime = _runtime(None)

    assert await tenant_links(runtime, auth) is None, "self-host has no builder"
    info = await _with_web_url(runtime, auth, _agent())
    assert "web_url" not in info.model_dump(mode="json"), "the output is what it was"
    assert "web_url" not in (await _viewed(runtime, auth, _routine())).model_dump(mode="json"), (
        "a routine carries no field either"
    )


async def test_an_agent_links_to_its_page_with_the_part_that_changed() -> None:
    auth = make_identity(tenant_id=_TENANT)

    info = await _with_web_url(_runtime(_WEB), auth, _agent(), "instructions")

    assert info.web_url == f"{_BASE}/agents/agent_1?node=instructions", "the workspace and node"


async def test_a_routine_links_to_its_drawer() -> None:
    view = await _viewed(_runtime(_WEB), make_identity(tenant_id=_TENANT), _routine())

    assert view.web_url == f"{_BASE}/routines?routine=aaaaaaaa-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("channel:19:abc@thread.tacv2", f"{_BASE}/channels?channel=19%3Aabc%40thread.tacv2"),
        ("workspace", None),
    ],
)
async def test_a_channel_scope_links_to_its_settings_and_a_workspace_scope_does_not(
    scope: str, expected: str | None
) -> None:
    result = ChannelEnvironmentResult(
        scope=scope, environment_name="e", previous_environment_name=None, changed=True, note="n"
    )

    linked = await _with_channel_link(_runtime(_WEB), make_identity(tenant_id=_TENANT), result)

    assert linked.web_url == expected, "a Teams id is encoded; the workspace default has no page"
    dumped = TypeAdapter(ChannelEnvironmentResult).dump_python(linked, mode="json")
    assert ("web_url" in dumped) is (expected is not None), "no link, no field"


async def test_a_member_gets_links_only_to_pages_a_member_may_open() -> None:
    member = make_identity(tenant_id=_TENANT, role=Role.USER)
    links = await tenant_links(_runtime(_WEB), member)

    assert links is not None, "the workspace has a web app"
    assert links.agent("agent_1") is not None, "members view agents"
    assert links.skill("skill_1") is None, "skills are for those who manage the workspace"
    assert links.person("u1") is None, "people are for admins"


async def test_set_agent_rule_links_to_the_agents_page() -> None:
    """The result carries the page of the agent the rule is about, from the id core returned."""
    from daimon.adapters.mcp.tools.channel_rules import SetAgentRuleResult, _with_agent_link

    result = SetAgentRuleResult(agent_name="stats", runs_in=None, changed=True, note="")
    auth = make_identity(tenant_id=_TENANT)
    linked = await _with_agent_link(_runtime(_WEB), auth, result, "agent_1")
    assert linked.web_url == f"{_BASE}/agents/agent_1", "the agent's own page"
    plain = await _with_agent_link(_runtime(None), auth, result, "agent_1")
    assert plain == result, "no web app, the result is what it was"
    gone = await _with_agent_link(_runtime(_WEB), auth, result, None)
    assert gone == result, "no live agent, no page and no lookup"


async def test_links_when_web_role_is_admin_without_chat_admin_then_admin_pages(
    web_role: dict[str, Any],
) -> None:
    web_role.update(person=SimpleNamespace(status="active"), role="admin")
    auth = make_identity(tenant_id=_TENANT, role=Role.USER)

    links = await tenant_links(_runtime(_WEB), auth)

    assert links is not None and links.person("u1") is not None, "the web role decides, not chat"


async def test_links_when_chat_admin_is_only_a_web_member_then_no_admin_pages(
    web_role: dict[str, Any],
) -> None:
    web_role.update(person=SimpleNamespace(status="active"), role="member")

    links = await tenant_links(_runtime(_WEB), make_identity(tenant_id=_TENANT))

    assert links is not None and links.agent("agent_1") is not None, "they may open the agents"
    assert links.person("u1") is None, "a chat admin who is a web member gets no admin page"


async def test_links_when_web_person_is_suspended_then_none(web_role: dict[str, Any]) -> None:
    web_role.update(person=SimpleNamespace(status="suspended"), role="admin")

    links = await tenant_links(_runtime(_WEB), make_identity(tenant_id=_TENANT))

    assert links is None, "a suspended person can open nothing, so no link is offered"


def test_admin_links_when_web_app_set_then_admin_pages_without_a_lookup() -> None:
    """A resolved admin gets the admin pages straight from `WebAdmin`, with no database."""
    actor = WebAdmin(tenant_id=_TENANT, account_id=uuid.uuid4(), role="admin", person_id=None)
    links = admin_links(_runtime(_WEB), actor)
    assert links is not None and links.audit() == f"{_BASE}/admin/audit", "the audit page"
    assert admin_links(_runtime(None), actor) is None, "no web app, no link"

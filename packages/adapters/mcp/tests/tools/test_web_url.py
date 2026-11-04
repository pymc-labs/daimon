"""Tool results link to the web app only when there is one and the caller may open the page."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._web_url import tenant_links
from daimon.adapters.mcp.tools.agents import (  # pyright: ignore[reportPrivateUsage]
    AgentInfo,
    _with_web_url,
)
from daimon.adapters.mcp.tools.channel_environments import (
    ChannelEnvironmentResult,
    _with_channel_link,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.routines import _viewed  # pyright: ignore[reportPrivateUsage]
from daimon.core.config import WebSettings
from daimon.core.stores.domain import Role, RoutineRow
from pydantic import TypeAdapter

from ..harness import make_identity

_NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)
_TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")
_BASE = f"https://app.example.com/w/{_TENANT}"


def _runtime(web: WebSettings | None) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(settings=SimpleNamespace(web=web)))


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


def test_no_web_app_means_no_links_and_no_field_in_the_output() -> None:
    auth = make_identity(tenant_id=_TENANT)
    runtime = _runtime(None)

    assert tenant_links(runtime, auth) is None, "self-host has no builder"
    info = _with_web_url(runtime, auth, _agent())
    assert "web_url" not in info.model_dump(mode="json"), "the output is what it was"
    assert "web_url" not in _viewed(runtime, auth, _routine()).model_dump(mode="json"), (
        "a routine carries no field either"
    )


def test_an_agent_links_to_its_page_with_the_part_that_changed() -> None:
    auth = make_identity(tenant_id=_TENANT)

    info = _with_web_url(_runtime(_WEB), auth, _agent(), "instructions")

    assert info.web_url == f"{_BASE}/agents/agent_1?node=instructions", "the workspace and node"


def test_a_routine_links_to_its_drawer() -> None:
    view = _viewed(_runtime(_WEB), make_identity(tenant_id=_TENANT), _routine())

    assert view.web_url == f"{_BASE}/routines?routine=aaaaaaaa-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("channel:19:abc@thread.tacv2", f"{_BASE}/channels?channel=19%3Aabc%40thread.tacv2"),
        ("workspace", None),
    ],
)
def test_a_channel_scope_links_to_its_settings_and_a_workspace_scope_does_not(
    scope: str, expected: str | None
) -> None:
    result = ChannelEnvironmentResult(
        scope=scope, environment_name="e", previous_environment_name=None, changed=True, note="n"
    )

    linked = _with_channel_link(_runtime(_WEB), make_identity(tenant_id=_TENANT), result)

    assert linked.web_url == expected, "a Teams id is encoded; the workspace default has no page"
    dumped = TypeAdapter(ChannelEnvironmentResult).dump_python(linked, mode="json")
    assert ("web_url" in dumped) is (expected is not None), "no link, no field"


def test_a_member_gets_links_only_to_pages_a_member_may_open() -> None:
    member = make_identity(tenant_id=_TENANT, role=Role.USER)
    links = tenant_links(_runtime(_WEB), member)

    assert links is not None, "the workspace has a web app"
    assert links.agent("agent_1") is not None, "members view agents"
    assert links.skill("skill_1") is None, "skills are for those who manage the workspace"
    assert links.person("u1") is None, "people are for admins"

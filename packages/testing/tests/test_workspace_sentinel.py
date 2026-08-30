from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsModelConfig
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_WORKSPACE,
    MA_METADATA_VALUE_WORKSPACE_DISPOSABLE,
)
from daimon.core.ma import WORKSPACE_SENTINEL_AGENT_NAME
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response
from daimon.testing.workspace_sentinel import (
    mark_workspace_disposable,
    require_disposable_workspace,
)


def _build_agent_crud_client(created: list[dict[str, Any]]) -> Any:
    """Transport-level client whose POST /v1/agents appends to the workspace.

    `created` doubles as the workspace roster returned by GET /v1/agents, so a
    second call to `mark_workspace_disposable` sees whatever the first created.
    """
    router = MARouter()

    def handle_agents_list(request: httpx.Request, match: Any) -> httpx.Response:
        return list_response(created)

    def handle_agent_create(request: httpx.Request, match: Any) -> httpx.Response:
        body = json_body(request)
        now = datetime.now(UTC).isoformat()
        agent = BetaManagedAgentsAgent(
            id=f"agent_created_{len(created)}",
            archived_at=None,
            created_at=now,
            description=body.get("description"),
            mcp_servers=[],
            metadata=body.get("metadata", {}),
            model=BetaManagedAgentsModelConfig(id=body["model"]),
            name=body["name"],
            skills=[],
            system=None,
            tools=[],
            type="agent",
            updated_at=now,
            version=1,
        ).model_dump(mode="json")
        created.append(agent)
        return httpx.Response(200, json=agent)

    router.add("GET", r"/v1/agents", handle_agents_list)
    router.add("POST", r"/v1/agents", handle_agent_create)
    return build_fake_anthropic(router.dispatch)


async def test_mark_workspace_disposable_creates_a_marked_sentinel_when_workspace_has_none() -> (
    None
):
    created: list[dict[str, Any]] = []
    client = _build_agent_crud_client(created)

    sentinel_id = await mark_workspace_disposable(client)

    assert len(created) == 1, "an unmarked workspace must gain exactly one sentinel agent"
    assert created[0]["id"] == sentinel_id, "the returned id must be the created sentinel's"
    assert created[0]["name"] == WORKSPACE_SENTINEL_AGENT_NAME, (
        "the sentinel must be findable by name in the MA console"
    )
    assert created[0]["metadata"] == {
        MA_METADATA_KEY_WORKSPACE: MA_METADATA_VALUE_WORKSPACE_DISPOSABLE
    }, "the marker metadata is what the nuke guard actually reads"


async def test_mark_workspace_disposable_reuses_existing_sentinel_when_called_twice() -> None:
    created: list[dict[str, Any]] = []
    client = _build_agent_crud_client(created)

    first_id = await mark_workspace_disposable(client)
    second_id = await mark_workspace_disposable(client)

    assert second_id == first_id, "a second call must adopt the existing sentinel"
    assert len(created) == 1, "marking twice must not accumulate duplicate sentinel agents"


async def test_require_disposable_workspace_fails_the_run_when_workspace_is_unmarked() -> None:
    created: list[dict[str, Any]] = []
    client = _build_agent_crud_client(created)

    with pytest.raises(pytest.fail.Exception) as exc_info:
        await require_disposable_workspace(client)

    assert "mark_disposable --yes" in str(exc_info.value), (
        "the banner must tell the operator how to mark a throwaway workspace"
    )
    assert created == [], "the guard must never mark the workspace on the caller's behalf"


async def test_require_disposable_workspace_returns_when_sentinel_is_present() -> None:
    created: list[dict[str, Any]] = []
    client = _build_agent_crud_client(created)
    await mark_workspace_disposable(client)

    await require_disposable_workspace(client)

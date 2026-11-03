"""`agent_refs`: the agents a channel names, shown by the agents list's own rule."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.web import channel_agents
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime


def _caller(*capabilities: str) -> WebCaller:
    person = SimpleNamespace(tenant_id=uuid.uuid4())
    return cast(WebCaller, SimpleNamespace(person=person, capabilities=frozenset(capabilities)))


@pytest.fixture(autouse=True)
def _agents(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = SimpleNamespace(id="ag_1", name="Analyst", metadata={})

    async def listed(_runtime: Any, _tenant_id: uuid.UUID) -> list[Any]:
        return [agent]

    async def avatars(_runtime: Any, _tenant_id: uuid.UUID, _agents: list[Any]) -> dict[str, str]:
        return {"ag_1": "face.png"}

    monkeypatch.setattr(channel_agents, "listed_agents", listed)
    monkeypatch.setattr(channel_agents, "avatars_of", avatars)


async def test_a_viewer_of_agents_gets_the_agent_with_its_face() -> None:
    refs = await channel_agents.agent_refs(
        cast(WebRuntime, None), _caller("agents.view"), ["Analyst", "Gone"]
    )
    assert refs["Analyst"].model_dump() == {"id": "ag_1", "name": "Analyst", "avatar": "face.png"}
    assert refs["Gone"].name is None and refs["Gone"].id is None


async def test_without_agents_view_an_agent_is_private_and_unlinked() -> None:
    refs = await channel_agents.agent_refs(
        cast(WebRuntime, None), _caller("channels.view"), ["Analyst", "Gone"]
    )
    assert refs["Analyst"].model_dump() == {"id": None, "name": "Private agent", "avatar": None}
    assert refs["Gone"].name is None, "a missing agent stays deleted, not private"

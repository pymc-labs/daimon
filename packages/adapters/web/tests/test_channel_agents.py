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


@pytest.mark.parametrize("reader_first", [True, False])
async def test_a_key_names_the_agent_that_owns_it_not_its_reader(
    monkeypatch: pytest.MonkeyPatch, reader_first: bool
) -> None:
    source = SimpleNamespace(id="ag_src", name="Atlas", metadata={"daimon_name": "atlas"})
    reader = SimpleNamespace(
        id="ag_rdr",
        name="Atlas Reader",
        metadata={
            "daimon_name": "atlas-reader",
            "daimon_reader_of": "ag_src",
            "daimon_reader_source": "Atlas\natlas",
        },
    )
    listing = [reader, source] if reader_first else [source, reader]

    async def listed(_runtime: Any, _tenant_id: uuid.UUID) -> list[Any]:
        return listing

    async def avatars(_runtime: Any, _tenant_id: uuid.UUID, _agents: list[Any]) -> dict[str, str]:
        return {}

    monkeypatch.setattr(channel_agents, "listed_agents", listed)
    monkeypatch.setattr(channel_agents, "avatars_of", avatars)
    refs = await channel_agents.agent_refs(
        cast(WebRuntime, None), _caller("agents.view"), ["Atlas", "atlas", "Atlas Reader"]
    )
    assert refs["Atlas"].id == refs["atlas"].id == "ag_src"
    assert refs["Atlas Reader"].id == "ag_rdr"


async def test_a_key_only_a_reader_borrows_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = SimpleNamespace(
        id="ag_rdr",
        name="Atlas Reader",
        metadata={"daimon_reader_of": "ag_src", "daimon_reader_source": "Atlas"},
    )

    async def listed(_runtime: Any, _tenant_id: uuid.UUID) -> list[Any]:
        return [reader]

    async def avatars(_runtime: Any, _tenant_id: uuid.UUID, _agents: list[Any]) -> dict[str, str]:
        return {}

    monkeypatch.setattr(channel_agents, "listed_agents", listed)
    monkeypatch.setattr(channel_agents, "avatars_of", avatars)
    refs = await channel_agents.agent_refs(
        cast(WebRuntime, None), _caller("agents.view"), ["Atlas"]
    )
    assert refs["Atlas"].id == "ag_rdr"

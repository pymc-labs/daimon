"""Contract tests for MA agent lifecycle: create, retrieve, update, list, archive."""

from __future__ import annotations

import uuid

import pytest
from anthropic import AsyncAnthropic

pytestmark = pytest.mark.contract


def _agent_name() -> str:
    return f"contract-test-agent-{uuid.uuid4().hex[:8]}"


async def test_agent_create_returns_expected_shape(anthropic_client: AsyncAnthropic) -> None:
    name = _agent_name()
    agent = await anthropic_client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system="contract test"
    )
    assert agent.id.startswith("agent_"), f"agent id must have 'agent_' prefix, got {agent.id!r}"
    assert agent.name == name, "name must round-trip"
    assert isinstance(agent.version, int), "version must be int"


async def test_agent_retrieve_matches_create(anthropic_client: AsyncAnthropic) -> None:
    name = _agent_name()
    created = await anthropic_client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system="contract test"
    )
    retrieved = await anthropic_client.beta.agents.retrieve(created.id)
    assert retrieved.id == created.id, "retrieved id must match created id"
    assert retrieved.name == created.name, "retrieved name must match created name"


async def test_agent_update_changes_system(anthropic_client: AsyncAnthropic) -> None:
    name = _agent_name()
    created = await anthropic_client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system="contract test"
    )
    updated = await anthropic_client.beta.agents.update(
        created.id, version=created.version, system="updated"
    )
    assert updated.system == "updated", f"system must be updated, got {updated.system!r}"
    assert updated.version > created.version, (
        f"version must increment after update, got {updated.version} (was {created.version})"
    )


async def test_agent_list_includes_created(anthropic_client: AsyncAnthropic) -> None:
    name = _agent_name()
    created = await anthropic_client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system="contract test"
    )
    ids = [a.id async for a in anthropic_client.beta.agents.list()]
    assert created.id in ids, f"created agent {created.id!r} must appear in list"


async def test_agent_archive_removes_from_default_list(anthropic_client: AsyncAnthropic) -> None:
    name = _agent_name()
    agent = await anthropic_client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system="contract test"
    )
    await anthropic_client.beta.agents.archive(agent.id)
    ids = [a.id async for a in anthropic_client.beta.agents.list()]
    assert agent.id not in ids, "archived agent must not appear in default list"

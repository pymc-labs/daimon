"""MCP resource extraction keeps the transport requests and retry boundary."""

from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from anthropic import ConflictError
from daimon.adapters.mcp.tools import skills
from daimon.testing.ma_models import ma_agent
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

TENANT = UUID("00000000-0000-0000-0000-000000000007")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000008")
AUTH = SimpleNamespace(tenant_id=TENANT, account_id=ACCOUNT)


def scripts(*replies):
    before, after = ScriptedTransport(), ScriptedTransport()
    for transport in (before, after):
        transport.queue(*replies)
    return before, after


def assert_equal(before, after):
    for transport in (before, after):
        transport.assert_consumed()
    assert [r.to_dict() for r in after.requests] == [r.to_dict() for r in before.requests]


@pytest.mark.parametrize("conflict", [False, True])
async def test_skill_attach_callsite_preserves_version_retry(monkeypatch, conflict):
    initial = ma_agent(
        id="agent1",
        tenant_id=TENANT,
        version=3,
        skills=[{"type": "anthropic", "skill_id": "xlsx", "version": "latest"}],
    )
    concurrent = ma_agent(
        id="agent1",
        tenant_id=TENANT,
        version=4,
        skills=[
            {"type": "anthropic", "skill_id": "xlsx", "version": "latest"},
            {"type": "custom", "skill_id": "concurrent", "version": "9"},
        ],
    )

    async def resolve(*args, **kwargs):
        return initial

    async def no_collision(*args, **kwargs):
        return None

    monkeypatch.setattr(skills, "resolve_setup_agent", resolve)
    monkeypatch.setattr(skills, "find_attach_mount_collision", no_collision)
    replies = [
        ScriptedReply(
            "GET", "/v1/agents/agent1", httpx.Response(200, json=initial.model_dump(mode="json"))
        )
    ]
    if conflict:
        replies += [
            ScriptedReply(
                "POST",
                "/v1/agents/agent1",
                httpx.Response(
                    409,
                    json={"error": {"type": "invalid_request_error", "message": "stale version"}},
                ),
            ),
            ScriptedReply(
                "GET",
                "/v1/agents/agent1",
                httpx.Response(200, json=concurrent.model_dump(mode="json")),
            ),
        ]
    replies += [
        ScriptedReply(
            "POST",
            "/v1/agents/agent1",
            httpx.Response(200, json=concurrent.model_dump(mode="json")),
        )
    ]
    before, after = scripts(*replies)
    async with before.client() as old, after.client() as new:
        fresh = await old.beta.agents.retrieve("agent1")
        try:
            await old.beta.agents.update(
                "agent1",
                version=fresh.version,
                skills=[
                    {"type": "custom", "skill_id": "added"},
                    {"type": "anthropic", "skill_id": "xlsx"},
                ],
            )
        except ConflictError:
            if not conflict:
                raise
            fresh = await old.beta.agents.retrieve("agent1")
            await old.beta.agents.update(
                "agent1",
                version=fresh.version,
                skills=[
                    {"type": "custom", "skill_id": "added"},
                    {"type": "anthropic", "skill_id": "xlsx"},
                    {"type": "custom", "skill_id": "concurrent"},
                ],
            )
        note = await skills._attach_synced_skills(
            SimpleNamespace(client=new), AUTH, agent_name="test-agent", skill_ids={"added"}
        )
    assert note == "Attached to 'test-agent'."
    assert_equal(before, after)


@pytest.mark.parametrize("description", [None, ""])
async def test_create_environment_callsite_preserves_omitted_and_empty_description(
    monkeypatch, description
):
    from daimon.adapters.mcp.tools import environments
    from daimon.core.defaults.metadata import build_metadata
    from daimon.core.specs import EnvironmentSpec
    from daimon.testing.ma_models import ma_environment

    async def no_collision(*args, **kwargs):
        return None

    monkeypatch.setattr(environments, "_reject_environment_name_collision", no_collision)
    spec = EnvironmentSpec(name="sandbox", description=description)
    record = ma_environment(id="env1", name="sandbox", tenant_id=TENANT).model_dump(mode="json")
    before, after = scripts(
        ScriptedReply("POST", "/v1/environments", httpx.Response(200, json=record))
    )
    async with before.client() as old, after.client() as new:
        payload = spec.model_dump(exclude_none=True)
        payload["metadata"] = build_metadata(tenant_id=TENANT, name=spec.name)
        expected = await old.beta.environments.create(**payload)
        actual = await environments._create_environment_impl(
            SimpleNamespace(client=new), AUTH, spec
        )
    assert actual == environments.EnvironmentInfo.from_ma(expected)
    assert_equal(before, after)

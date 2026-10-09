"""The neutral adapter edge preserves refusal copy and native request bytes."""

# Parity tests deliberately exercise private host boundaries.
# pyright: reportPrivateUsage=false
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import httpx
import pytest
from anthropic import ConflictError
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_skill_params import BetaManagedAgentsSkillParams
from daimon.adapters.teams import add_skill
from daimon.adapters.teams import credential_repos as credentials
from daimon.adapters.teams.card_actions import Actor
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.roster import RosterAgent
from daimon.core.skills.ingest import SkillBundle
from daimon.testing.ma_models import ma_agent
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport


async def test_a_foreign_reread_keeps_the_existing_refusal_and_does_not_upload() -> None:
    tenant = UUID("00000000-0000-0000-0000-000000000007")
    foreign = UUID("00000000-0000-0000-0000-000000000009")
    account = UUID("00000000-0000-0000-0000-000000000008")
    record = ma_agent(id="agent1", name="project", tenant_id=foreign).model_dump(mode="json")
    (before, after) = (ScriptedTransport(), ScriptedTransport())
    for transport in (before, after):
        transport.queue(ScriptedReply("GET", "/v1/agents/agent1", httpx.Response(200, json=record)))
    async with before.client() as old, after.client() as new:
        native = await old.beta.agents.retrieve("agent1")
        assert native.metadata["daimon_tenant"] != str(tenant)
        with pytest.raises(add_skill.SkillAddRefused) as refusal:
            await add_skill.add_previewed_skill(
                cast(TeamsRuntime, SimpleNamespace(anthropic=new)),
                cast(Actor, SimpleNamespace(tenant_id=tenant)),
                cast(RosterAgent, SimpleNamespace(name="project", ma_agent_id="agent1")),
                cast(SkillBundle, SimpleNamespace()),
                account_id=account,
            )
    assert refusal.value.refusal == "project is not an agent of this organisation."
    for transport in (before, after):
        transport.assert_consumed()
    assert [r.to_dict() for r in after.requests] == [r.to_dict() for r in before.requests]


@pytest.mark.parametrize("status", ["success", "retry", "exhausted"])
async def test_imported_skill_attachment_keeps_requests_retry_limit_and_notice(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    tenant = UUID("00000000-0000-0000-0000-000000000007")
    initial = ma_agent(
        id="agent1",
        name="project",
        tenant_id=tenant,
        version=3,
        skills=[{"type": "anthropic", "skill_id": "xlsx", "version": "latest"}],
    )
    concurrent = ma_agent(
        id="agent1",
        name="project",
        tenant_id=tenant,
        version=4,
        skills=[
            {"type": "anthropic", "skill_id": "xlsx", "version": "latest"},
            {"type": "custom", "skill_id": "existing", "version": "9"},
        ],
    )

    async def resolve(*args: object, **kwargs: object) -> BetaManagedAgentsAgent:
        return initial

    async def no_collision(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(credentials, "find_agent_by_derived_uuid", resolve)
    monkeypatch.setattr(credentials, "find_attach_mount_collision", no_collision)
    replies = [
        ScriptedReply(
            "GET", "/v1/agents/agent1", httpx.Response(200, json=initial.model_dump(mode="json"))
        )
    ]
    if status != "success":
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
    code = 409 if status == "exhausted" else 200
    record = (
        {"error": {"type": "invalid_request_error", "message": "stale version"}}
        if code == 409
        else concurrent.model_dump(mode="json")
    )
    replies += [ScriptedReply("POST", "/v1/agents/agent1", httpx.Response(code, json=record))]
    (before, after) = (ScriptedTransport(), ScriptedTransport())
    for transport in (before, after):
        transport.queue(*replies)
    async with before.client() as old, after.client() as new:
        fresh = await old.beta.agents.retrieve("agent1")
        initial_payload: list[BetaManagedAgentsSkillParams] = [
            {"type": "custom", "skill_id": "added"},
            {"type": "anthropic", "skill_id": "xlsx"},
        ]
        try:
            await old.beta.agents.update("agent1", version=fresh.version, skills=initial_payload)
        except ConflictError:
            fresh = await old.beta.agents.retrieve("agent1")
            try:
                await old.beta.agents.update(
                    "agent1",
                    version=fresh.version,
                    skills=[*initial_payload, {"type": "custom", "skill_id": "existing"}],
                )
            except ConflictError:
                assert status == "exhausted"
        outcomes = [
            ResourceOutcome(kind="skill", name="added", action=Action.CREATED, anthropic_id="added")
        ]
        result = await credentials.attach_imported_skills(
            cast(TeamsRuntime, SimpleNamespace(anthropic=new)),
            tenant_id=tenant,
            agent_id=UUID("00000000-0000-0000-0000-000000000008"),
            outcomes=outcomes,
        )
    assert result.attached is (status != "exhausted")
    assert result.note == (
        "Attaching them did not finish. Ask again to retry."
        if status == "exhausted"
        else "Attached 1 to `project`."
    )
    for transport in (before, after):
        transport.assert_consumed()
    assert [r.to_dict() for r in after.requests] == [r.to_dict() for r in before.requests]

"""Executable record: how a followed server skill update reaches each provider.

A web agent is a Managed Agents record, so the one sync target repins it there.
An agent switched to OpenAI or Gemini runs that record under the owner's drop
consent: its consent is renewed only when the declared drops stay identical, else
the agent is held as "Needs re-confirm". Those providers get no custom skills
unless an operator representation pins the exact source, and a repin always
outdates that pin, so such an agent is held too. Self-hosted runs a web agent
only in an environment an operator granted that agent, so an agent without a
grant cannot follow on it. Change this record with the behaviour, never delete it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.server_skills import sync_deps
from daimon.core.channel_backend import AgentBackendRefused, AgentSettings, granted_settings
from daimon.core.config import Settings, WebSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.server_skills.anthropic import ManagedAgentsTarget
from daimon.core.server_skills.consent import ConsentKeeper, with_skill_version
from daimon.core.server_skills.node import SkillsNode
from daimon.core.stores.domain import BackendAgent, SourceRepresentation
from daimon.core.turn.runtime_manifest import preview_source_drops, source_preservation_pin
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope, SkillRef
from mux.contracts.resources import Agent, AgentSpec, Environment, EnvironmentSpec
from mux.errors import UnsupportedCapability
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import async_sessionmaker

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _source() -> BackendAgent:
    tenant = uuid.uuid4()
    skills = (SkillRef(id="skill_1", version="1", source="custom"),)
    spec = AgentSpec(
        name="helper",
        model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
        system="Help.",
        skills=skills,
    )
    ref = ResourceRef(
        provider="anthropic",
        kind="agent",
        id="agent",
        account_scope_id="workspace",
        tenant_id=str(tenant),
        account_id="a",
    )
    environment = Environment(
        ref=ref.model_copy(update={"kind": "environment", "id": "env"}),
        revision=Revision(local=1, native="1"),
        spec=EnvironmentSpec(name="box"),
        created_at=NOW,
    )
    return BackendAgent(
        tenant_id=tenant,
        agent_id=uuid.uuid4(),
        name="helper",
        configuration_digest="digest",
        source=Agent(ref=ref, revision=Revision(local=1, native="1"), spec=spec, created_at=NOW),
        effective_spec=spec,
        resource_scope=Scope(
            tenant_id=str(tenant), account_id="a", principal_id="p", authorization_id="s"
        ),
        environment=environment,
        memory_required=False,
        memory_read_only=True,
        asks_before_publishing=False,
    )


async def test_the_managed_agents_record_is_the_one_target_and_checks_every_consent() -> None:
    """Every follower is repinned on its record, through its backend consent."""
    async with httpx.AsyncClient() as http:
        runtime = WebRuntime(
            settings=Settings(_env_file=None, database={"url": "postgresql+asyncpg://u:p@h/db"}),  # pyright: ignore[reportCallIssue, reportArgumentType]
            web=WebSettings(public_url="https://web.example.com"),  # pyright: ignore[reportCallIssue, reportArgumentType]
            sessionmaker=async_sessionmaker(),
            http_client=http,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            anthropic=build_fake_anthropic(MARouter().dispatch),
        )
        node = SimpleNamespace(server=SimpleNamespace(name="data"))
        deps = sync_deps(runtime, cast(SkillsNode, node))
    assert [type(t) for t in deps.targets] == [ManagedAgentsTarget]
    target = cast(ManagedAgentsTarget, deps.targets[0])
    assert isinstance(target.consents, ConsentKeeper), "no follower skips its consent check"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_other_providers_drop_custom_skills_so_a_version_keeps_their_drops(provider: str) -> None:
    """The consent's drops name skills, never a version, so a repin can keep them."""
    source = _source()
    model = ModelRef(provider=provider, id="model")
    before = preview_source_drops(source, model)
    after = preview_source_drops(with_skill_version(source, "skill_1", "2"), model)
    assert before.skills == () and "skills" in before.declared_drops, "custom skills are dropped"
    assert after.declared_drops == before.declared_drops, "a new version keeps the same drops"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_an_operator_representation_is_outdated_by_any_repin(provider: str) -> None:
    """A representation pins the exact source, so a repinned agent is held for re-confirm."""
    source = _source()
    model = ModelRef(provider=provider, id="model")
    spec = source.effective_spec
    assert spec is not None
    represented = SourceRepresentation(
        target=AgentSpec(name=spec.name, model=model, system=spec.system, skills=spec.skills),
        source_pin=source_preservation_pin(source),
    )
    held = source.model_copy(update={"representations": (represented,)})
    assert preview_source_drops(held, model).skills == spec.skills, "it maps skills as pinned"
    with pytest.raises(UnsupportedCapability, match="source_representation"):
        preview_source_drops(with_skill_version(held, "skill_1", "2"), model)


def test_self_hosted_refuses_a_web_agent_without_the_operators_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-hosted is an agent model only where an operator granted the agent an environment."""
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(tmp_path / "runtime.json"))
    with pytest.raises(AgentBackendRefused, match="selfhosted requires channel configuration"):
        granted_settings(_source(), "selfhosted", None, AgentSettings())

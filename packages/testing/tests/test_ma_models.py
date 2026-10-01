"""Tests for the canonical MA shape builders in daimon.testing.ma_models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_mcp_toolset import BetaManagedAgentsMCPToolset
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.pricing import AGENT_MODEL_PRICING
from daimon.testing.ma_models import (
    DEFAULT_AGENT_ID,
    DEFAULT_ENV_ID,
    DEFAULT_MODEL_ID,
    DEFAULT_SESSION_ID,
    EMPTY_CLOUD_CONFIG,
    FIXED_TS,
    FIXED_TS_STR,
    ma_agent,
    ma_environment,
    ma_model_config,
    ma_model_usage,
    ma_session,
    ma_session_agent,
    ma_session_agent_from,
    tenant_metadata,
)


def test_defaults_validate_as_real_sdk_models() -> None:
    assert isinstance(ma_agent(), BetaManagedAgentsAgent), "ma_agent() must be a real SDK agent"
    assert isinstance(ma_environment(), BetaEnvironment), "ma_environment() must be a real env"
    assert isinstance(ma_session_agent(), BetaManagedAgentsSessionAgent), (
        "ma_session_agent() must be a real session-agent snapshot"
    )
    assert isinstance(ma_session(), BetaManagedAgentsSession), "ma_session() must be a real session"
    assert isinstance(ma_model_config(), BetaManagedAgentsModelConfig), (
        "ma_model_config() must be a real model config"
    )
    assert isinstance(ma_model_usage(), BetaManagedAgentsSpanModelUsage), (
        "ma_model_usage() must be a real span usage"
    )


def test_default_model_is_metered_by_the_pricing_table() -> None:
    assert DEFAULT_MODEL_ID in AGENT_MODEL_PRICING, (
        "a faked turn on the default model must reach the ledger, so the default must be metered"
    )


def test_defaults_agree_across_shapes() -> None:
    session = ma_session()
    agent = ma_agent()
    assert session.agent.id == agent.id == DEFAULT_AGENT_ID, (
        "a default session must freeze the default responder's id (a mismatch reads as a handoff)"
    )
    assert session.agent.model.id == agent.model.id == DEFAULT_MODEL_ID, (
        "a default session must freeze the default responder's model"
    )
    assert session.environment_id == ma_environment().id == DEFAULT_ENV_ID, (
        "a default session must run on the default environment"
    )
    assert session.id == DEFAULT_SESSION_ID, "session id default must be the shared constant"
    assert FIXED_TS.isoformat().replace("+00:00", "Z") == FIXED_TS_STR, (
        "the datetime and string spellings of the fixed timestamp must name the same instant"
    )


def test_ma_session_freezes_a_given_agent() -> None:
    frozen = ma_session(agent=ma_agent(id="x", name="named", model="claude-opus-4-8"))
    assert frozen.agent.id == "x", "ma_session(agent=ma_agent(id='x')) must freeze id 'x'"
    assert frozen.agent.name == "named", "the frozen snapshot must carry the agent's name"
    assert frozen.agent.model.id == "claude-opus-4-8", "the frozen snapshot must carry the model"

    snapshot = ma_session_agent(id="y")
    assert ma_session(agent=snapshot).agent is snapshot, (
        "a BetaManagedAgentsSessionAgent must be used as-is"
    )


def test_ma_session_shortcuts_set_agent_id_and_model() -> None:
    session = ma_session(agent_id="ag_x", model="claude-opus-4-8")
    assert session.agent.id == "ag_x", "agent_id= must set the frozen agent id"
    assert session.agent.model.id == "claude-opus-4-8", "model= must set the frozen model"


def test_ma_session_rejects_agent_together_with_shortcuts() -> None:
    with pytest.raises(ValueError, match="either agent= or agent_id="):
        ma_session(agent=ma_agent(), agent_id="ag_x")


def test_ma_session_agent_from_copies_the_agent_definition() -> None:
    agent = ma_agent(
        id="ag_src",
        name="src",
        system="be brief",
        description="d",
        version=3,
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "srv",
                "configs": [],
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
            }
        ],
        mcp_servers=[{"type": "url", "name": "srv", "url": "https://mcp.example"}],
        skills=[{"type": "custom", "skill_id": "sk_1", "version": "1"}],
    )
    frozen = ma_session_agent_from(agent)
    for attr in ("id", "name", "system", "description", "version", "model"):
        assert getattr(frozen, attr) == getattr(agent, attr), f"{attr} must be copied verbatim"
    assert [t.model_dump() for t in frozen.tools] == [t.model_dump() for t in agent.tools], (
        "tools must be copied"
    )
    assert frozen.mcp_servers == agent.mcp_servers, "mcp_servers must be copied"
    assert frozen.skills == agent.skills, "skills must be copied"


def test_dict_form_tools_round_trip_through_validation() -> None:
    agent = ma_agent(
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "srv",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_ask"}},
            }
        ],
        mcp_servers=[{"type": "url", "name": "srv", "url": "https://mcp.example"}],
    )
    assert isinstance(agent.tools[0], BetaManagedAgentsMCPToolset), (
        "dict-form tools must validate into the SDK tool model"
    )
    dumped = agent.model_dump(mode="json")
    assert dumped["tools"][0]["mcp_server_name"] == "srv", "dumped tool must keep its fields"
    assert BetaManagedAgentsAgent.model_validate(dumped) == agent, (
        "model_dump(mode='json') must round-trip through model_validate"
    )


def test_dict_form_tools_missing_required_fields_are_rejected() -> None:
    with pytest.raises(ValueError):
        ma_agent(tools=[{"type": "mcp_toolset", "mcp_server_name": "srv"}])


def test_model_accepts_string_or_config() -> None:
    assert ma_agent(model="claude-opus-4-8").model.id == "claude-opus-4-8", (
        "a model string must become a BetaManagedAgentsModelConfig"
    )
    config = ma_model_config("claude-opus-4-8", speed="fast")
    assert ma_agent(model=config).model is config, "a model config must be used as-is"
    assert ma_session_agent(model=config).model.speed == "fast", (
        "session agents must accept the same model forms"
    )


def test_tenant_id_stamps_resolver_metadata_and_explicit_metadata_merges_over_it() -> None:
    tenant_id = uuid.uuid4()
    agent = ma_agent(tenant_id=tenant_id)
    assert agent.metadata == {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_NAME: agent.name,
    }, "tenant_id= must stamp the tenant and name keys the resolver matches on"

    merged = ma_agent(tenant_id=tenant_id, metadata={MA_METADATA_KEY_NAME: "other", "k": "v"})
    assert merged.metadata == {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_NAME: "other",
        "k": "v",
    }, "explicit metadata= must win over the tenant_id= convenience, key by key"

    assert ma_agent().metadata == {}, "no tenant_id and no metadata must give empty metadata"
    assert ma_environment(tenant_id="t").metadata == {
        MA_METADATA_KEY_TENANT: "t",
        MA_METADATA_KEY_NAME: ma_environment().name,
    }, "environments stamp the same keys"


def test_tenant_metadata_carries_extra_keys() -> None:
    assert tenant_metadata("t", "n", daimon_managed="true") == {
        MA_METADATA_KEY_TENANT: "t",
        MA_METADATA_KEY_NAME: "n",
        "daimon_managed": "true",
    }, "extra keyword stamps must be included verbatim"


def test_json_dumps_round_trip_through_model_validate() -> None:
    now = datetime(2026, 6, 14, tzinfo=UTC)
    agent = ma_agent(tenant_id="t", created_at=now, archived_at=now)
    assert BetaManagedAgentsAgent.model_validate(agent.model_dump(mode="json")) == agent, (
        "agent JSON must round-trip"
    )
    environment = ma_environment(tenant_id="t", description="d", scope="account")
    assert BetaEnvironment.model_validate(environment.model_dump(mode="json")) == environment, (
        "environment JSON must round-trip"
    )
    session = ma_session(
        agent=agent,
        status="running",
        metadata={"k": "v"},
        title="t",
        vault_ids=["vault_1"],
        created_at=now,
    )
    assert BetaManagedAgentsSession.model_validate(session.model_dump(mode="json")) == session, (
        "session JSON must round-trip"
    )


def test_updated_at_defaults_to_created_at() -> None:
    now = datetime(2026, 6, 14, tzinfo=UTC)
    assert ma_agent(created_at=now).updated_at == now, "agent updated_at must track created_at"
    assert ma_session(created_at=now).updated_at == now, "session updated_at must track created_at"
    assert ma_environment(created_at="2026-06-14T00:00:00Z").updated_at == "2026-06-14T00:00:00Z", (
        "environment updated_at must track created_at"
    )


def test_environment_defaults_to_the_shared_cloud_config() -> None:
    assert ma_environment().config is EMPTY_CLOUD_CONFIG, (
        "the default environment config must be the shared constant"
    )

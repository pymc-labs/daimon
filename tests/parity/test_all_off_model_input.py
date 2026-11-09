"""Every `model` feature Off, on each backend: the agent gets only the turn's framing, the
safety block, its own system prompt and the person's text. No tools, no skills.

Honest exceptions are the backend-fixed values the registry declares (`EXCEPTIONS`): Gemini
and OpenAI agents always carry Daimon's tools and their own skills, and Daimon's tools lift
Minimal turn controls to Origin because the tools need the conversation ids.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from anthropic.types.beta import BetaManagedAgentsCustomSkill
from daimon.core.agent_guidance import SAFETY_BLOCK, apply_credential_guidance
from daimon.core.features.model_input import SessionToolset, guidance_applies, turn_controls
from daimon.core.features.registry import FEATURES
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.session_identity import SessionIdentity
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot, resolve_one
from daimon.core.session_snapshot import session_mcp_servers, session_skills, session_tools
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn_origin import render_turn_origin
from daimon.testing.ma_models import ma_agent
from mux.profiles import INLINE_REUSE, MANAGED_AGENTS, PERSISTENT_WORKSPACE

BACKENDS = (MANAGED_AGENTS.provider, INLINE_REUSE.provider, PERSISTENT_WORKSPACE.provider)
EXCEPTIONS = {
    INLINE_REUSE.provider: {"daimon_tools": "on", "agent_skills": "on"},
    PERSISTENT_WORKSPACE.provider: {"daimon_tools": "on", "agent_skills": "on"},
}
PUBLIC = "https://daimon.example/mcp"
SYSTEM = "You are the stats agent."
_SKILL = BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_platform", version="1")
_CHANNEL = BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_channel", version="1")


def _all_off(backend: str) -> FeatureSnapshot:
    place = FeaturePlace(uuid.uuid4(), "slack", "c1", agent_id="agt", backend=backend)
    found: dict[str, ResolvedFeature] = {}
    for key, spec in FEATURES.items():
        default = resolve_one(spec, None, place, [])
        if spec.effect != "model" or default.unavailable == "backend":
            found[key] = default
        else:
            off = "minimal" if key == "turn_controls" else "off"
            found[key] = ResolvedFeature(key, off, "channel")
    return FeatureSnapshot(place, 1, found)


def _agent() -> Any:
    allow = {"enabled": True, "permission_policy": {"type": "always_allow"}}
    return ma_agent(
        id="agt",
        system=SYSTEM,
        tools=[
            {"type": "agent_toolset_20260401", "default_config": allow, "configs": []},
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": allow,
                "configs": [],
            },
        ],
        mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": PUBLIC}],
        skills=[{"type": "custom", "skill_id": "skill_own", "version": "1"}],
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_model_feature_off_leaves_only_the_base_input(backend: str) -> None:
    features, agent = _all_off(backend), _agent()
    fixed = EXCEPTIONS.get(backend, {})
    model_keys = [key for key, spec in FEATURES.items() if spec.effect == "model"]
    for key in model_keys:
        expected = fixed.get(key, "minimal" if key == "turn_controls" else "off")
        assert features.value(key) == expected, f"{backend}: {key} is Off or a declared exception"

    toolset = SessionToolset.of(features, {})
    assert toolset.builtins and not any(toolset.builtins.values()), "every builtin tool is Off"
    assert not toolset.env_file, "no keys file"
    assert toolset.daimon_tools == ("daimon_tools" in fixed), "Daimon tools only where fixed"
    tools = session_tools(
        agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=PUBLIC, toolset=toolset
    )
    servers = session_mcp_servers(
        agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=PUBLIC, toolset=toolset
    )
    enabled = [
        config
        for tool in (t.model_dump(mode="json") for t in tools)
        if tool["type"] == "agent_toolset_20260401"
        for config in tool.get("configs", [])
        if config.get("enabled")
    ]
    assert enabled == [], "no builtin tool is enabled"
    assert [s.name for s in servers] == (["daimon-mcp"] if fixed else []), "no Daimon server"

    identity = SessionIdentity.of(features)
    assert identity.extra_skills((_SKILL, _CHANNEL), frozenset({"skill_platform"})) == ()
    own = session_skills(agent, (), agent_skills=identity.agent_skills)
    assert [s.skill_id for s in own] == (["skill_own"] if fixed else []), "no agent skills"
    assert identity.system(SYSTEM, "slack") is None, "the agent's own prompt, unframed"
    guided = apply_credential_guidance(
        SYSTEM, guidance=guidance_applies(isolated=False, features=features)
    )
    assert guided == f"{SAFETY_BLOCK}\n\n{SYSTEM}", "only the safety block above the prompt"

    level = turn_controls(features)
    assert level == ("origin" if fixed else "minimal"), "Minimal unless Daimon tools need ids"
    origin = TurnOriginRow.model_validate(
        {
            "id": uuid.UUID(int=1),
            "tenant_id": uuid.UUID(int=2),
            "account_id": uuid.UUID(int=3),
            "platform": "slack",
            "parent_channel_id": "C1",
            "thread_id": "T1",
            "responder_ma_agent_id": "agt",
            "responder_name": "stats",
            "configuration_target_ma_agent_id": None,
            "configuration_target_name": None,
            "role": Role.USER,
            "is_setup": False,
            "created_at": "2026-09-13T00:00:00Z",
            "expires_at": "2026-09-13T02:00:00Z",
        }
    )
    rendered = render_turn_origin(origin, effective_is_admin=False, features=features)
    assert '"words"' not in rendered and '"web"' not in rendered, "no words or web links"

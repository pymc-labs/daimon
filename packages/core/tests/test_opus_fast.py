"""Offline coverage for the selectable native fast preset and its tariff."""

import pytest
from daimon.core.constants import ALLOWED_MODEL_IDS
from daimon.core.model_presets import OPUS, OPUS_FAST, model_choice_id, resolve_model
from daimon.core.models_catalog import claude_model_choices
from daimon.core.mux_compat import agent_spec
from daimon.core.pricing import MODEL_PRICING
from daimon.core.session_snapshot import SessionSnapshot, fingerprint_identity
from daimon.core.specs import AgentSpec
from mux.drivers.anthropic.resources.agents import agent_payload


def test_fast_is_a_distinct_catalog_choice():
    assert OPUS in ALLOWED_MODEL_IDS and OPUS_FAST in ALLOWED_MODEL_IDS
    choice = next(c for c in claude_model_choices(default=OPUS_FAST) if c.id == OPUS_FAST)
    assert choice.label == "Opus 5.5 fast"
    assert choice.is_default


@pytest.mark.parametrize("as_object", [False, True])
def test_preset_compiles_to_native_model(as_object):
    model = {"id": OPUS_FAST, "effort": "high", "inference_geo": "us"} if as_object else OPUS_FAST
    expected = {"id": OPUS, "speed": "fast"}
    if as_object:
        expected.update(effort="high", inference_geo="us")
    authored = AgentSpec(name="fast", model=model)
    assert authored.model == expected
    assert agent_payload(agent_spec({"name": "fast", "model": model}))["model"] == expected
    assert model_choice_id(expected) == OPUS_FAST
    if as_object:
        assert model["id"] == OPUS_FAST  # Caller-owned config is untouched.


def test_standard_model_stays_standard():
    assert resolve_model(OPUS) == OPUS
    assert model_choice_id({"id": OPUS, "speed": "standard"}) == OPUS


def test_fast_session_keeps_native_id_and_own_tariff():
    values = dict(
        ma_agent_id="agent",
        agent_version=1,
        agent_name="fast",
        model_id=OPUS,
        system_sha256=None,
        skills_sha256="skills",
        environment_id="env",
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        tools_sha256="tools",
        mcp_servers_sha256="mcp",
        env_sha256=None,
    )
    standard = SessionSnapshot(**values)
    fast = SessionSnapshot(**values, model_speed="fast")
    assert fast.model_id == standard.model_id == OPUS
    assert standard.pricing_id == OPUS
    assert fast.pricing_id == OPUS_FAST
    assert fingerprint_identity(fast) != fingerprint_identity(standard)
    assert SessionSnapshot.model_validate(fast.model_dump()).pricing_id == OPUS_FAST
    assert SessionSnapshot.model_validate(values).pricing_id == OPUS


@pytest.mark.parametrize("stage", ["input", "output", "cache_write", "cache_read"])
def test_fast_tariff_doubles_every_token_stage(stage):
    assert getattr(MODEL_PRICING[OPUS_FAST], stage) == 2 * getattr(MODEL_PRICING[OPUS], stage)

"""Constants tests. SYSTEM_ACCOUNT_ID was removed in the tenant migration."""

from __future__ import annotations

import inspect
from pathlib import Path

import yaml
from daimon.adapters.discord.agent_setup import new_agent as discord_new_agent
from daimon.adapters.mcp.tools import agents as mcp_agents_tool
from daimon.adapters.mcp.tools import skills as mcp_skills_tool
from daimon.adapters.slack.agent_setup import submit as slack_submit
from daimon.core.constants import (
    AGENT_MCP_CAP,
    AGENT_SKILL_CAP,
    ALLOWED_MODEL_IDS,
    DEFAULT_AGENT_MODEL,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_agent_caps_are_positive_ints() -> None:
    assert isinstance(AGENT_SKILL_CAP, int) and AGENT_SKILL_CAP > 0, (
        "AGENT_SKILL_CAP must be a positive int"
    )
    assert isinstance(AGENT_MCP_CAP, int) and AGENT_MCP_CAP > 0, (
        "AGENT_MCP_CAP must be a positive int"
    )


def test_adapter_modules_declare_no_private_cap_literal_of_their_own() -> None:
    """Both surfaces must read the one shared constant.

    A value-equality test (e.g. asserting some adapter-local number == 20)
    would keep passing if a future edit reintroduced a second private literal
    that happened to match today's cap — drift would go undetected until the
    two numbers diverged. Asserting the source itself carries no
    `_SKILL_CAP =` / `_MCP_CAP =` assignment catches the reintroduction at the
    moment it happens, not only when it later disagrees.
    """
    for module in (mcp_skills_tool, mcp_agents_tool):
        source = inspect.getsource(module)
        assert "_SKILL_CAP =" not in source, (
            f"{module.__name__} must not declare a private _SKILL_CAP literal"
        )
        assert "_MCP_CAP =" not in source, (
            f"{module.__name__} must not declare a private _MCP_CAP literal"
        )


def test_default_agent_model_is_selectable() -> None:
    assert DEFAULT_AGENT_MODEL in ALLOWED_MODEL_IDS, (
        "the model new agents get by default must itself pass the panel's submit-time "
        f"validation; {DEFAULT_AGENT_MODEL} is not in ALLOWED_MODEL_IDS"
    )


def test_current_generation_opus_and_sonnet_are_both_selectable() -> None:
    """The two models a user asks for by word must both resolve to something allowed.

    Someone saying "use Opus" gets `claude-opus-5-5`; the panel validates free-text
    input against ALLOWED_MODEL_IDS, so an id missing from the pricing table is
    refused at submit with no hint that the model exists.
    """
    for model in ("claude-opus-5-5", "claude-sonnet-5-5"):
        assert model in ALLOWED_MODEL_IDS, f"{model} must be selectable as an agent model"


def test_fable_5_2_is_selectable_but_never_the_default() -> None:
    """Fable 5.2 is offered by name; adding it must not move anyone's default.

    It lists at four times Sonnet 5.5's token price, so a new agent only runs on
    it when someone picks it.
    """
    assert "claude-fable-5-2" in ALLOWED_MODEL_IDS, "fable 5.2 must be selectable"
    assert DEFAULT_AGENT_MODEL == "claude-sonnet-5-5", (
        f"new agents must keep defaulting to Sonnet 5.5, not {DEFAULT_AGENT_MODEL}"
    )


def test_fable_5_2_is_not_the_first_picker_option() -> None:
    """Pickers list ALLOWED_MODEL_IDS in order, so the top option is the first row.

    Fable 5.2 is the priciest model offered; it sits below Opus 5.5 so it is never
    the first thing someone sees in the list.
    """
    assert ALLOWED_MODEL_IDS[0] == "claude-opus-5-5", "Opus 5.5 leads the picker list"
    assert ALLOWED_MODEL_IDS.index("claude-fable-5-2") > ALLOWED_MODEL_IDS.index(
        "claude-opus-5-5"
    ), "fable 5.2 must be listed below opus 5.5"


def test_default_agent_model_matches_the_seeded_agent() -> None:
    """The default new agents get must equal the model `defaults/` ships.

    These drifted apart once: the seeded agent moved to claude-sonnet-5 while three
    adapter surfaces kept a private "claude-sonnet-4-6" literal, so creating an
    agent through chat or the panel produced one a generation behind the bot the
    same install already ran, with nothing surfacing the difference.
    """
    seeded = yaml.safe_load((REPO_ROOT / "defaults" / "agents" / "daimon.yaml").read_text())
    assert seeded["model"] == DEFAULT_AGENT_MODEL, (
        f"defaults/agents/daimon.yaml pins {seeded['model']} but new agents default to "
        f"{DEFAULT_AGENT_MODEL} — the two must move together"
    )


def test_agent_surfaces_declare_no_private_model_literal_of_their_own() -> None:
    """Same reasoning as the cap test: catch a reintroduced literal, not a divergence.

    Asserting equality against today's model id would keep passing if an edit
    reintroduced a hardcoded "claude-..." default that happens to match, and would
    only fail later once the two drifted — which is exactly how this bug shipped.
    """
    for module in (discord_new_agent, slack_submit):
        source = inspect.getsource(module)
        assert '"claude-' not in source, (
            f"{module.__name__} must read DEFAULT_AGENT_MODEL, not hardcode a model id"
        )

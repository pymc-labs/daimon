"""Gemini managed agent with inline environment reuse.

Not core: the inline environment expires after inactivity, so a thread's
workspace is not guaranteed to persist. Expiry surfaces as `ContinuityLost`,
never as a silent fresh start, and a channel has to name this profile to get
it.
"""

from __future__ import annotations

from mux.contracts.extensions import ExtensionRef
from mux.contracts.profile import AccountingMode, Effort, Profile, SkillLimits

# Opt-in model: one attempt, never the Flash fallback chain.
ARGON_MODEL = "gemini-4-argon"

# The agent's thinking level; the neutral names match.
_EFFORT: dict[Effort, str] = {"low": "low", "medium": "medium", "high": "high"}
MIB = 1024 * 1024

INLINE_REUSE = Profile(
    provider="gemini",
    profile_id="gemini.inline_reuse",
    carries_caller_resources=True,
    schema_version="1",
    accounting_mode=AccountingMode.NATIVE_TOTALS,
    sdk_pin="google-genai>=2.7.0,<3",
    core=False,
    effort_map=_EFFORT,
    extensions=(ExtensionRef(namespace="gemini.session", version=1),),
    execution_tool="code_execution",
    mcp_policy_keys=("allowed_tools",),
    skill_digest="version",
    # Inline mounts: UTF-8 text, 1 MiB a file, 2 MiB across the session.
    skill_limits=SkillLimits(
        total_bytes=2 * MIB, file_bytes=MIB, files=256, text_only=True, combined_bytes=2 * MIB
    ),
    catalog_readback="verify",
    support={
        "neutral_catalog": "emulated",
        "input_token_limit": "unsupported",
        "output_token_limit": "native",
        "agent_delete": "unsupported",
        "thread_workspace_persistence": "unsupported",
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        # source-G-runtime.txt pending function results + pinned SDK Usage schema;
        # tests/drivers/gemini/test_core.py proves actions, nulls and revisions.
        "required_actions": "native",
        "usage_observations": "emulated",
        # source-G-agent.txt inline SKILL.md + source-G-env.txt Files snapshot;
        # tests/drivers/gemini/test_resources.py checks pins, bytes and scope.
        "skills_bundle": "emulated",
        "artifacts": "emulated",
        "vaults": "unsupported",
        "memory_stores": "unsupported",
        "steer": "unsupported",
        "tool_confirmation": "unsupported",
        "native_event_replay": "unknown",
        # Stored interactions can be re-read; lossless delta replay is unknown.
        "reconcile": "emulated",
        "event_previews": "native",
        "multiagent": "unsupported",
        "native_schedules": "unsupported",  # no trigger port in this driver
    },
)

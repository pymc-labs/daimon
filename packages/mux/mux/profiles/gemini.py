"""Gemini managed agent with inline environment reuse.

Not core: the inline environment expires after inactivity, so a thread's
workspace is not guaranteed to persist. Expiry surfaces as `ContinuityLost`,
never as a silent fresh start, and a channel has to name this profile to get
it.
"""

from __future__ import annotations

from mux.contracts.extensions import ExtensionRef
from mux.contracts.profile import AccountingMode, Profile

WORKER_MODEL = "gemini-3.5-flash-lite"
# Opt-in model: one attempt, never the Flash fallback chain. A channel runs it
# directly; a fleet worker runs it only on the bounded token envelope.
ARGON_MODEL = "gemini-4-argon"
# A whole unbounded worker turn (input + output). At the highest output tariff it
# may run on (Flash), its worst case stays under the $0.49 worker hold.
WORKER_MAX_TOTAL_TOKENS = 65_536
WORKER_MAX_RUNTIME_S = 600

INLINE_REUSE = Profile(
    provider="gemini",
    profile_id="gemini.inline_reuse",
    schema_version="1",
    accounting_mode=AccountingMode.NATIVE_TOTALS,
    sdk_pin="google-genai>=2.7.0,<3",
    core=False,
    extensions=(ExtensionRef(namespace="gemini.session", version=1),),
    execution_tool="code_execution",
    mcp_policy_keys=("allowed_tools",),
    skill_digest="version",
    catalog_readback="verify",
    support={
        "neutral_catalog": "emulated",
        "input_token_limit": "emulated",
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

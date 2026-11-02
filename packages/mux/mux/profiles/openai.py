"""OpenAI Agents API profiles.

`persistent_workspace` runs on a hosted environment; its resource ports are
verified against native HTTP/SSE fixtures.
`conversation_only` has no workspace, so it is not core and a channel has
to name it to get it.
"""

from __future__ import annotations

from mux.contracts.extensions import OPENAI_EXTENSIONS
from mux.contracts.profile import AccountingMode, Profile

LUNA_MODEL = "gpt-6-luna"
ASTRA_MODEL = "gpt-6-astra"
# Every model the native OpenAI host runs; the admitted revision selects one.
NATIVE_MODELS = (LUNA_MODEL, ASTRA_MODEL)
# GPT-5.6 and later bill cache writes at 1.25x uncached input (OpenAI prompt-caching
# guide), but Agents API turn usage "do[es] not expose a separate cache-write count,
# so they cannot determine the exact model charge when that pricing applies" (Agents
# API observability). The driver's meter therefore never reports one.
_METERS_CACHE_WRITES = False

PERSISTENT_WORKSPACE = Profile(
    provider="openai",
    profile_id="openai.persistent_workspace",
    schema_version="1",
    accounting_mode=AccountingMode.NATIVE_ROOT_TOTALS,
    meters_cache_writes=_METERS_CACHE_WRITES,
    sdk_pin="openai>=2.54.0,<3",
    core=True,  # Nine mandatory capabilities have actual-driver evidence; see driver README.
    execution_tool="bash",
    implicit_execution=True,
    mcp_policy_keys=("allowed_tools", "required"),
    tool_readback="compiled",
    skill_digest="content",
    support={
        "neutral_catalog": "native",
        "agent_delete": "native",
        # The Agents reference documents `spend_control`, but our project answers it
        # with HTTP 400 unknown_parameter on every create (2026-10-23, Ref Y7D94P):
        # no native session cap, so nothing may count on one.
        "session_spend_control": "unsupported",
        "thread_workspace_persistence": "native",
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        "required_actions": "native",
        # Inline ZIP upload -> agent intent -> concrete session skill pins;
        # test_skill_workflow proves the installed version survives two turns.
        "skills_bundle": "native",
        "skills_versions": "native",
        # Resource coverage: all pages, exact binary bytes, typed truncation
        # and scoped input/session artifact deletion through the actual SDK.
        "artifacts": "native",
        "usage_observations": "native",
        # Missed events cannot be replayed; state is rebuilt from saved items.
        "reconcile": "emulated",
        "steer": "native",
        "native_event_replay": "unsupported",
        "event_previews": "native",
        # Actual-SDK tests verify owned metadata, host-resolved MCP credentials
        # and redacted debug logs; archive/conditional writes refuse explicitly.
        "vaults": "native",
        # Initial file mounts exist; conditional replacement does not.
        "session_resources": "unsupported",
        # Child event/usage decoding does not provision a multiagent roster;
        # drivers/openai/agents.py refuses that configuration (2026-10-09).
        "multiagent": "unsupported",
        # The 2026-10-09 Agents docs verify published artifacts, not a complete
        # workspace export/restore mapping. Both session ports refuse it.
        "workspace_export_import": "unknown",
    },
    extensions=OPENAI_EXTENSIONS,
)

CONVERSATION_ONLY = Profile(
    provider="openai",
    profile_id="openai.conversation_only",
    schema_version="1",
    accounting_mode=AccountingMode.NATIVE_ROOT_TOTALS,
    meters_cache_writes=_METERS_CACHE_WRITES,
    sdk_pin="openai>=2.54.0,<3",
    core=False,
    support={
        "neutral_catalog": "native",
        "agent_delete": "native",
        # Rejected by our project like the persistent profile's (Ref Y7D94P).
        "session_spend_control": "unsupported",
        "thread_workspace_persistence": "unsupported",
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        "required_actions": "native",
        "skills_bundle": "unsupported",
        "usage_observations": "native",
        "reconcile": "emulated",
        "steer": "native",
        "native_event_replay": "unsupported",
        "event_previews": "native",
        # Resource access is independent of the conversation-only environment;
        # these actual-SDK ports enforce the same host scope authorization.
        "artifacts": "native",
        "vaults": "native",
        # No multiagent configuration port is implemented; child decoding alone
        # is not evidence of this capability (driver tests, 2026-10-09).
        "multiagent": "unsupported",
        "workspace_export_import": "unsupported",
    },
    extensions=OPENAI_EXTENSIONS,
)

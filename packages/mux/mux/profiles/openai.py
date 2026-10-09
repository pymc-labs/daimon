"""OpenAI Agents API profiles.

`persistent_workspace` runs on a hosted environment; its resource ports are
verified against native HTTP/SSE fixtures.
`conversation_only` has no workspace, so it is not core and a channel has
to name it to get it.
"""

from __future__ import annotations

from mux.contracts.extensions import OPENAI_EXTENSIONS
from mux.contracts.profile import Profile

PERSISTENT_WORKSPACE = Profile(
    provider="openai",
    profile_id="openai.persistent_workspace",
    schema_version="1",
    sdk_pin="openai>=2.54.0,<3",
    core=True,  # Nine mandatory capabilities have actual-driver evidence; see driver README.
    support={
        "thread_workspace_persistence": "native",
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        "required_actions": "native",
        # Inline ZIP upload -> agent intent -> concrete session skill pins;
        # test_skill_workflow proves the installed version survives two turns.
        "skills_bundle": "native",
        "skills_versions": "native",
        # C09 plus test_resources: all pages, exact binary bytes, typed truncation
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
        # Initial file mounts exist; conditional replacement does not (C08).
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
    sdk_pin="openai>=2.54.0,<3",
    core=False,
    support={
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

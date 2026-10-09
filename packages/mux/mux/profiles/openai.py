"""OpenAI Agents API profiles.

`persistent_workspace` runs on a hosted environment and is core.
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
    core=True,
    support={
        "thread_workspace_persistence": "native",
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        "required_actions": "native",
        "skills_bundle": "native",
        "artifacts": "native",
        "usage_observations": "native",
        # Missed events cannot be replayed; state is rebuilt from saved items.
        "reconcile": "emulated",
        "steer": "native",
        "native_event_replay": "unsupported",
        "event_previews": "native",
        "vaults": "native",
        "multiagent": "native",
        "workspace_export_import": "native",
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
        "vaults": "native",
        "multiagent": "native",
        "workspace_export_import": "unsupported",
    },
    extensions=OPENAI_EXTENSIONS,
)

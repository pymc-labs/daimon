"""OpenAI Agents API profiles.

`persistent_workspace` runs on a hosted environment; its core resource ports
are still being implemented.
`conversation_only` has no workspace, so it is not core and a channel has
to name it to get it.
"""

from __future__ import annotations

from mux.contracts.extensions import OPENAI_EXTENSIONS
from mux.contracts.profile import Profile

_IMPLEMENTED_EXTENSIONS = tuple(
    extension for extension in OPENAI_EXTENSIONS if extension.namespace == "openai.steer"
)

PERSISTENT_WORKSPACE = Profile(
    provider="openai",
    profile_id="openai.persistent_workspace",
    schema_version="1",
    sdk_pin="openai>=2.54.0,<3",
    core=False,  # core once skills_bundle/artifacts land (PR3)
    support={
        "thread_workspace_persistence": "native",
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        "required_actions": "native",
        # Default resource ports are absent in the core slice; PR3 must supply
        # tested inline bundles and exact binary artifact transfer.
        "skills_bundle": "unsupported",
        "artifacts": "unsupported",
        "usage_observations": "native",
        # Missed events cannot be replayed; state is rebuilt from saved items.
        "reconcile": "emulated",
        "steer": "native",
        "native_event_replay": "unsupported",
        "event_previews": "native",
        # The vault port is absent until the resource slice; injection is not
        # evidence that the default driver implements it (2026-10-09).
        "vaults": "unsupported",
        # Child event/usage decoding does not provision a multiagent roster;
        # drivers/openai/agents.py refuses that configuration (2026-10-09).
        "multiagent": "unsupported",
        # The 2026-10-09 Agents docs verify published artifacts, not a complete
        # workspace export/restore mapping. Both session ports refuse it.
        "workspace_export_import": "unknown",
    },
    extensions=_IMPLEMENTED_EXTENSIONS,
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
        # The default vault port is not implemented in the core slice.
        "vaults": "unsupported",
        # No multiagent configuration port is implemented; child decoding alone
        # is not evidence of this capability (driver tests, 2026-10-09).
        "multiagent": "unsupported",
        "workspace_export_import": "unsupported",
    },
    extensions=_IMPLEMENTED_EXTENSIONS,
)

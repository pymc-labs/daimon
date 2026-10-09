"""Anthropic Managed Agents: the core profile Daimon runs on today."""

from __future__ import annotations

from mux.contracts.extensions import ANTHROPIC_EXTENSIONS
from mux.contracts.profile import Profile

MANAGED_AGENTS = Profile(
    provider="anthropic",
    profile_id="anthropic.managed_agents",
    schema_version="1",
    sdk_pin="anthropic>=0.117",
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
        "reconcile": "native",
        "steer": "native",
        "tool_confirmation": "native",
        "native_event_replay": "native",
        "event_previews": "native",
        "vaults": "native",
        "memory_stores": "native",
        "session_resources": "native",
        "skills_versions": "native",
        "multiagent": "native",
        "environments_fork": "native",
        "native_schedules": "native",
        "model_request_usage": "native",
        "workspace_export_import": "native",
    },
    extensions=ANTHROPIC_EXTENSIONS,
)

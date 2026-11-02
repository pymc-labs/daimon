"""Anthropic Managed Agents: the core profile Daimon runs on today."""

from __future__ import annotations

from mux.contracts.extensions import ANTHROPIC_EXTENSIONS, ExtensionRef
from mux.contracts.ids import SkillRef
from mux.contracts.profile import AccountingMode, Profile

#: The Managed Agents model object (id, speed) carried as an agent extension.
MODEL_CONFIG_NAMESPACE = "anthropic.model_config"

MANAGED_AGENTS = Profile(
    provider="anthropic",
    profile_id="anthropic.managed_agents",
    schema_version="1",
    accounting_mode=AccountingMode.REQUEST_SPANS,
    sdk_pin="anthropic>=0.117",
    core=True,
    emits_legacy_events=True,
    support={
        "thread_workspace_persistence": "native",
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        "required_actions": "native",
        "skills_bundle": "native",
        "artifacts": "native",
        "usage_observations": "native",
        # Core, but `AnthropicEvents.reconcile` still raises UnsupportedCapability; the
        # profile honesty probe tracks it as a pending overclaim.
        "reconcile": "native",
        # Unsupported by this driver: it refuses steering input and a stream cursor,
        # and has no environment-fork or schedule port.
        "steer": "unsupported",
        "tool_confirmation": "native",
        "native_event_replay": "unsupported",
        "event_previews": "native",
        "vaults": "native",
        "memory_stores": "native",
        "session_resources": "native",
        "skills_versions": "native",
        "multiagent": "native",
        "environments_fork": "unsupported",
        "native_schedules": "unsupported",
        "model_request_usage": "native",
        "session_spend_control": "native",
        "workspace_export_import": "native",
    },
    extensions=(*ANTHROPIC_EXTENSIONS, ExtensionRef(namespace=MODEL_CONFIG_NAMESPACE, version=1)),
)


def is_builtin_skill(ref: SkillRef) -> bool:
    """Recognize native built-ins even when an imported ref omits its source."""
    return ref.source == "anthropic" or (
        ref.source is None and ref.id in {"docx", "pptx", "xlsx", "pdf"}
    )

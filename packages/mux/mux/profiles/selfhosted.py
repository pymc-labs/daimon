"""Self-hosted harness profile, explicitly selected and fail-closed.

HTTP driver tests cover turn lifecycle, approvals, cancellation and durable replay.
Usage remains unwaivable at admission; missing measurements never become zero.
"""

from mux.contracts.profile import AccountingMode, Profile

LOOP = Profile(
    provider="selfhosted",
    profile_id="selfhosted.loop",
    schema_version="1",
    sdk_pin="daimon-harness/0",
    core=False,
    accounting_mode=AccountingMode.NATIVE_REQUESTS,
    execution_tool="bash",
    implicit_execution=True,
    support={
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        "required_actions": "native",
        "usage_observations": "native",
        "model_request_usage": "native",
        "reconcile": "native",
        "native_event_replay": "native",
        "event_previews": "native",
        "tool_confirmation": "native",
        "steer": "unsupported",
        "multiagent": "unsupported",
        "vaults": "unsupported",
        "memory_stores": "unsupported",
        "workspace_export_import": "unsupported",
        "skills_versions": "unsupported",
    },
)

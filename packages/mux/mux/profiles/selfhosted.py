"""Self-hosted harness profile, explicitly selected and fail-closed.

Runtime capabilities remain unknown until the driver has conformance evidence.
Usage is unwaivable at admission, including while this profile is a scaffold.
"""

from mux.contracts.profile import AccountingMode, Profile

LOOP = Profile(
    provider="selfhosted",
    profile_id="selfhosted.loop",
    schema_version="1",
    sdk_pin="daimon-harness/0",
    core=False,
    accounting_mode=AccountingMode.NATIVE_TOTALS,
    execution_tool="bash",
    support={
        "steer": "unsupported",
        "multiagent": "unsupported",
        "vaults": "unsupported",
        "memory_stores": "unsupported",
        "workspace_export_import": "unsupported",
        "skills_versions": "unsupported",
    },
)

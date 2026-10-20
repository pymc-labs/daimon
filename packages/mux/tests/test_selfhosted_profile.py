"""Self-hosted profile selection and unwaivable metering."""

import pytest
from mux.contracts.admission import admit
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import PROVIDERS, ChannelRef
from mux.errors import InvalidConfig, UnsupportedCapability
from mux.profiles import (
    backend_selection,
    get_profile,
    runnable_selection,
    shared_threads_available,
)


def test_selfhosted_selection_requires_explicit_profile_and_allows_deployment_names() -> None:
    assert "selfhosted" in PROVIDERS
    selected = backend_selection("selfhosted")
    assert (selected.profile, selected.model, selected.thread_mode) == (
        "selfhosted.loop",
        "gpt-5",
        "per_caller",
    )
    assert runnable_selection(selected)
    assert runnable_selection(selected.model_copy(update={"model": "tenant-gpt5"}))
    assert not shared_threads_available("selfhosted")
    assert not runnable_selection(selected.model_copy(update={"thread_mode": "shared"}))
    with pytest.raises(InvalidConfig, match="no default profile"):
        resolve_default(BackendConfig(backend="selfhosted", model="gpt-5"))


def test_selfhosted_refuses_profiles_without_usage() -> None:
    profile = get_profile("selfhosted.loop")
    assert not profile.core
    for capability in (
        "steer",
        "multiagent",
        "vaults",
        "memory_stores",
        "workspace_export_import",
        "skills_versions",
    ):
        assert profile.support[capability] == "unsupported"
    config = ConfigRevision.create(
        ChannelRef(tenant_id="tenant", platform="test", channel_id="channel"),
        1,
        backend_selection("selfhosted"),
    )
    with pytest.raises(UnsupportedCapability, match="usage_observations"):
        admit(
            config,
            profile.model_copy(
                update={"support": {**profile.support, "usage_observations": "unsupported"}}
            ),
        )

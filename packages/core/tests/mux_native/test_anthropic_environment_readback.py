"""Anthropic environment readback through the Daimon host record mapping."""

import pytest


@pytest.mark.parametrize("extra", [None, "execution_identity", "future_policy", "state"])
def test_environment_readback_metadata_is_excluded_but_unknown_authority_refuses(extra):
    from daimon.core.mux_compat import source_environment_record
    from mux.contracts.ids import Scope
    from mux.testing.drivers.anthropic.ma_models import ma_environment

    native = ma_environment().model_copy(update={"state": "active"})
    if extra:
        native = native.model_copy(update={extra: {"type": "unknown-authority"}})
    record = source_environment_record(
        native,
        Scope(
            tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
        ),
    )
    assert record.native["state"] == native.model_dump(mode="json")["state"]
    if extra:
        assert record.spec.native_config.value["unclassified_fields"] == [extra]
    else:
        assert record.spec.native_config is None
        assert record.spec.execution == "hosted"
        assert record.spec.network.mode == "unrestricted"
        assert dict(record.spec.packages) == {"system": (), "python": (), "npm": ()}


@pytest.mark.parametrize(
    "state", ["archived", "deleted", "Active", "active ", "", None, True, 1, {}, []]
)
def test_non_active_environment_lifecycle_remains_a_source_obligation(state):
    from daimon.core.mux_compat import source_environment_record
    from mux.contracts.ids import Scope
    from mux.testing.drivers.anthropic.ma_models import ma_environment

    native = ma_environment().model_copy(update={"state": state})
    record = source_environment_record(
        native,
        Scope(
            tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
        ),
    )
    assert record.spec.native_config is not None
    assert record.spec.native_config.value["unclassified_fields"] == ["state"]
    assert record.native["state"] == state

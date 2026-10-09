import pytest
from mux.contracts.ids import ResourceRef, Scope
from pydantic import ValidationError


@pytest.mark.parametrize(
    "scope",
    [
        Scope(
            tenant_id="tenant",
            account_id="account",
            principal_id="host",
            authorization_id="authorized",
        ),
        Scope.platform(reason="defaults sweep"),
        Scope.legacy_host_authorized(call_site="daimon.core.ma:delete_skill_and_versions"),
    ],
)
def test_scope_kinds_round_trip_without_conflation(scope):
    decoded = Scope.model_validate_json(scope.model_dump_json())
    assert decoded == scope
    assert decoded.is_platform == (decoded.platform_reason is not None)
    assert decoded.is_legacy_host_authorized == (decoded.legacy_call_site is not None)
    assert not (decoded.is_platform and decoded.is_legacy_host_authorized)


@pytest.mark.parametrize("value", ["", "   "])
def test_privileged_scope_requires_a_reason_or_named_caller(value):
    with pytest.raises(ValidationError):
        Scope.platform(reason=value)
    with pytest.raises(ValidationError):
        Scope.legacy_host_authorized(call_site=value)


def test_scope_cannot_claim_both_legacy_and_platform_authority():
    with pytest.raises(ValidationError):
        Scope(
            tenant_id="tenant",
            account_id="account",
            principal_id="host",
            authorization_id="auth",
            platform_reason="sweep",
            legacy_call_site="helper",
        )


@pytest.mark.parametrize("stamped", [False, True])
def test_resource_ref_ownership_round_trip(stamped):
    ref = ResourceRef(
        id="id",
        kind="agent",
        provider="anthropic",
        account_scope_id="workspace",
        **({"tenant_id": "tenant", "account_id": "account"} if stamped else {}),
    )
    assert ResourceRef.model_validate_json(ref.model_dump_json()) == ref

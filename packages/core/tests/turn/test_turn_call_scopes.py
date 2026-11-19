"""An admitted turn binds the real tenant and account scope."""

from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.run import _turn_port_kwargs  # pyright: ignore[reportPrivateUsage]
from daimon.testing.turn_fakes import deps_for
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation

TENANT = UUID(int=601)
ACCOUNT = UUID(int=602)
SCOPE = Scope(
    tenant_id=str(TENANT),
    account_id=str(ACCOUNT),
    principal_id="daimon",
    authorization_id="admitted-turn",
)


def test_prepared_turn_binds_the_real_account() -> None:
    admission = cast(
        Admission, SimpleNamespace(account_id=ACCOUNT, grant=None, backend_revision=None)
    )
    deps = replace(deps_for(None, None), backend=None)
    args = _turn_port_kwargs(deps, admission, "session", tenant_id=TENANT)
    assert args == {"scope": SCOPE}


def test_prepared_turn_refuses_a_grant_from_another_tenant() -> None:
    grant = cast(AdmissionGrant, SimpleNamespace(tenant_id=UUID(int=603)))
    admission = cast(Admission, SimpleNamespace(account_id=ACCOUNT, grant=grant))
    deps = replace(deps_for(None, None), backend=None)
    with pytest.raises(ScopeViolation):
        _turn_port_kwargs(deps, admission, "session", tenant_id=TENANT)

"""Additive mount methods reject untrusted references before provider I/O."""

import pytest
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import ResourceBinding
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.sessions_admin import AnthropicSessionAdmin
from mux.errors import ScopeViolation

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)


def ref(kind, native_id):
    return ResourceRef(
        id=native_id,
        kind=kind,
        provider="anthropic",
        account_scope_id="org",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )


@pytest.mark.parametrize("violation", ["session_tenant", "file_tenant", "file_grant", "scope"])
async def test_additive_file_add_checks_scope_and_both_references(violation):
    transport = ScriptedTransport()
    session, file = ref("session", "sess_1"), ref("file", "file_1")
    scope = SCOPE
    grants = {("session", session.id), ("file", file.id)}
    if violation == "session_tenant":
        session = session.model_copy(update={"tenant_id": "other"})
    elif violation == "file_tenant":
        file = file.model_copy(update={"tenant_id": "other"})
    elif violation == "file_grant":
        grants.remove(("file", file.id))
    else:
        scope = scope.model_copy(update={"account_id": "other"})
    async with transport.client() as client:
        port = AnthropicSessionAdmin(
            client, "org", authorization=ResourceAuthorization(SCOPE, frozenset(grants))
        )
        with pytest.raises(ScopeViolation):
            await port.add_file(
                scope,
                session,
                ResourceBinding(
                    id="new",
                    kind="artifact",
                    resource=file,
                    target_path=".env",
                ),
                key="add",
            )
    assert transport.requests == []


async def test_additive_resource_walk_checks_the_session_before_io():
    transport = ScriptedTransport()
    async with transport.client() as client:
        port = AnthropicSessionAdmin(client, "org", authorization=ResourceAuthorization(SCOPE))
        with pytest.raises(ScopeViolation):
            _ = [binding async for binding in port.walk(SCOPE, ref("session", "ungranted"))]
    assert transport.requests == []

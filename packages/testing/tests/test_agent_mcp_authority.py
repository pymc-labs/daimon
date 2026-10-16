"""Foreign provider ACKs cannot substitute their ids for canonical caller authority."""

from dataclasses import replace

import pytest
from daimon.testing.anthropic_live import LiveRefused
from daimon.testing.f2_mcp_binding import AgentMCPAuthority, AgentMCPBinding
from mux.contracts.ids import ResourceRef, Scope


@pytest.mark.parametrize(
    "defect",
    [
        "unacknowledged",
        "canonical_missing",
        "foreign_account",
        "foreign_tenant",
        "canonical_provider",
    ],
)
async def test_foreign_ack_requires_exact_canonical_association_before_signer(tmp_path, defect):
    scope = Scope(
        tenant_id="tenant", account_id="account", principal_id="owner", authorization_id="ack"
    )
    native = ResourceRef(
        provider="gemini",
        kind="agent",
        id="native",
        account_scope_id="project",
        tenant_id="tenant",
        account_id="account",
    )
    canonical = native.model_copy(update={"provider": "anthropic", "id": "actual-source"})
    if defect == "foreign_account":
        canonical = canonical.model_copy(update={"account_id": "foreign"})
    elif defect == "foreign_tenant":
        canonical = canonical.model_copy(update={"tenant_id": "foreign"})
    elif defect == "canonical_provider":
        canonical = native
    authority = AgentMCPAuthority(
        provider="gemini",
        scope=scope,
        account_scope_id="project",
        output=tmp_path,
        deadline_s=600,
        database_url="postgresql+asyncpg://not-opened",
        mcp_signing_secret_file=tmp_path / "not-read",
        acknowledged=lambda _: defect != "unacknowledged",
        redact=lambda _: pytest.fail("must not read secrets"),
        canonical_source=canonical,
    )
    if defect == "canonical_missing":
        authority = replace(authority, canonical_source=None)
    binding = AgentMCPBinding(authority=authority)
    with pytest.raises(LiveRefused):
        await binding.bind(native)
    assert binding.engine is None and binding.jti is None and not list(tmp_path.iterdir())
    await binding.close()

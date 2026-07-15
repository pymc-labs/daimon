"""Unit tests for daimon.core.ma_identity.derive_agent_uuid and derive_tenant_uuid."""

from __future__ import annotations

import uuid

from daimon.core.ma_identity import (
    _DAIMON_AGENT_NS,  # pyright: ignore[reportPrivateUsage]  # test pins the frozen namespace constants
    _DAIMON_TENANT_NS,  # pyright: ignore[reportPrivateUsage]  # test pins the frozen namespace constants
    derive_agent_uuid,
    derive_tenant_uuid,
)


def test_derive_is_deterministic_for_same_inputs() -> None:
    """Same (tenant, ma_agent_id) must always derive the same UUID."""
    tenant = uuid.UUID("11111111-1111-1111-1111-111111111111")
    ma_id = "agent_017vXaNG5P7Fu1g4orggSwEY"
    r1 = derive_agent_uuid(tenant_id=tenant, ma_agent_id=ma_id)
    r2 = derive_agent_uuid(tenant_id=tenant, ma_agent_id=ma_id)
    assert r1 == r2, "derive_agent_uuid must be deterministic — panel and MCP broker depend on this"


def test_derive_differs_across_ma_agent_ids() -> None:
    """Different MA ids under the same tenant derive different UUIDs."""
    tenant = uuid.UUID("11111111-1111-1111-1111-111111111111")
    r1 = derive_agent_uuid(tenant_id=tenant, ma_agent_id="agent_aaa")
    r2 = derive_agent_uuid(tenant_id=tenant, ma_agent_id="agent_bbb")
    assert r1 != r2, "two distinct agents must not collide on the local UUID"


def test_derive_differs_across_tenants() -> None:
    """Same MA id under different tenants derives different UUIDs (tenant scoping)."""
    ma_id = "agent_017vXaNG5P7Fu1g4orggSwEY"
    r1 = derive_agent_uuid(
        tenant_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        ma_agent_id=ma_id,
    )
    r2 = derive_agent_uuid(
        tenant_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        ma_agent_id=ma_id,
    )
    assert r1 != r2, "tenant scoping must isolate derived UUIDs across tenants"


def test_derive_handles_prefixed_string_id() -> None:
    """MA returns `agent_<base32>`, not a UUID. derive_agent_uuid must accept it."""
    tenant = uuid.uuid4()
    result = derive_agent_uuid(tenant_id=tenant, ma_agent_id="agent_017vXaNG5P7Fu1g4orggSwEY")
    assert isinstance(result, uuid.UUID), "derive_agent_uuid must always return a UUID"
    assert result.version == 5, "must be uuid5 — uuid4 would not be deterministic"


def test_derive_against_frozen_vector() -> None:
    """Golden-vector test: pins the namespace UUID + algorithm.

    If this assertion changes, you broke backward compatibility — every
    agent_repo_binding row keyed under the old namespace is now orphaned.
    Don't 'fix' this test; if you really need a re-key, write a migration.
    """
    tenant = uuid.UUID("11111111-1111-1111-1111-111111111111")
    ma_id = "agent_017vXaNG5P7Fu1g4orggSwEY"
    expected = uuid.UUID("209d06ce-e511-5c0d-b780-61332d6436a7")
    assert derive_agent_uuid(tenant_id=tenant, ma_agent_id=ma_id) == expected, (
        "frozen vector — namespace changed? See test docstring."
    )


# --- derive_tenant_uuid tests ---


def test_derive_tenant_uuid_is_deterministic_when_same_inputs() -> None:
    """Same (platform, workspace_id) must always derive the same tenant UUID."""
    r1 = derive_tenant_uuid(platform="discord", workspace_id="g1")
    r2 = derive_tenant_uuid(platform="discord", workspace_id="g1")
    assert r1 == r2, (
        "derive_tenant_uuid must be deterministic — same inputs must always produce the same UUID"
    )


def test_derive_tenant_uuid_differs_when_workspace_differs() -> None:
    """Different workspace_ids under the same platform derive different tenant UUIDs."""
    r1 = derive_tenant_uuid(platform="discord", workspace_id="g1")
    r2 = derive_tenant_uuid(platform="discord", workspace_id="g2")
    assert r1 != r2, "distinct workspace_ids must produce distinct tenant UUIDs"


def test_derive_tenant_uuid_differs_from_agent_uuid_namespace() -> None:
    """derive_tenant_uuid and derive_agent_uuid use distinct namespaces — no collision possible."""
    tenant_uuid = derive_tenant_uuid(platform="discord", workspace_id="g1")
    agent_uuid = derive_agent_uuid(
        tenant_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        ma_agent_id="agent_017vXaNG5P7Fu1g4orggSwEY",
    )
    assert tenant_uuid != agent_uuid, "tenant and agent uuid5 domains must be independent"
    assert _DAIMON_TENANT_NS != _DAIMON_AGENT_NS, (
        "tenant namespace must be distinct from agent namespace"
    )


def test_derive_tenant_uuid_cli_sentinel_equals_uuid5_of_cli_local() -> None:
    """CLI sentinel derive_tenant_uuid(platform='cli', workspace_id='local') must equal uuid5(_DAIMON_TENANT_NS, 'cli:local')."""
    result = derive_tenant_uuid(platform="cli", workspace_id="local")
    expected = uuid.uuid5(_DAIMON_TENANT_NS, "cli:local")
    assert result == expected, "CLI sentinel must equal uuid5(_DAIMON_TENANT_NS, 'cli:local')"


def test_derive_tenant_uuid_differs_when_platform_differs() -> None:
    """Same workspace_id under different platforms must derive different tenant UUIDs."""
    r1 = derive_tenant_uuid(platform="discord", workspace_id="123456")
    r2 = derive_tenant_uuid(platform="slack", workspace_id="123456")
    assert r1 != r2, (
        "distinct platforms with the same workspace_id must produce distinct tenant UUIDs "
        "(the key is 'platform:workspace_id', not workspace_id alone)"
    )


def test_derive_tenant_uuid_slack_is_deterministic() -> None:
    """Same (platform="slack", workspace_id) must always derive the same tenant UUID."""
    r1 = derive_tenant_uuid(platform="slack", workspace_id="T123")
    r2 = derive_tenant_uuid(platform="slack", workspace_id="T123")
    assert r1 == r2, (
        "derive_tenant_uuid must be deterministic for slack — "
        "same inputs must always produce the same UUID"
    )


def test_derive_tenant_uuid_slack_distinct_from_other_platforms() -> None:
    """platform="slack" with the same workspace_id must differ from "discord" and "cli"."""
    slack = derive_tenant_uuid(platform="slack", workspace_id="T123")
    discord = derive_tenant_uuid(platform="discord", workspace_id="T123")
    cli = derive_tenant_uuid(platform="cli", workspace_id="T123")
    assert slack != discord, (
        "slack and discord with the same workspace_id must produce distinct tenant UUIDs"
    )
    assert slack != cli, (
        "slack and cli with the same workspace_id must produce distinct tenant UUIDs"
    )

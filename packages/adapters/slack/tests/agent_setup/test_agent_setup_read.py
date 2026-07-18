"""Real-Postgres tests for agent_setup/read.py.

Three behaviors (security invariant + roster cap + scope-hint copy):
(a) Names-only secret hygiene: load_section_data(section="secrets") returns key
    NAMES only — the secret value never appears in the result.
(b) Roster cap-25 over_cap: load_tenant_roster caps at 25 entries when more
    agents exist in the tenant.
(c) Scope-hint copy: load_scope_hint returns the UI-SPEC copy per scope state
    (workspace-scope hit and unset case).
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from daimon.adapters.slack.agent_setup.read import (
    load_scope_hint,
    load_section_data,
    load_tenant_roster,
)
from daimon.core._models import Account, Tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import TenantScopeRef
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEAM_ID = "T_READ_TESTS"
_AGENT_NAME = "read-test-agent"
_MA_AGENT_ID = f"agent_{'x' * 24}"


async def _seed_tenant(session: AsyncSession, team_id: str = _TEAM_ID) -> uuid.UUID:
    """Create a Tenant row and return the derived tenant_id."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    session.add(Tenant(id=tenant_id, platform="slack", external_id=team_id))
    await session.flush()
    return tenant_id


async def _seed_account(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Create an Account row and return its id."""
    account = Account(tenant_id=tenant_id, role="user")
    session.add(account)
    await session.flush()
    return account.id  # type: ignore[return-value]  # SA mapped column UUID


def _make_agent_payload(
    *,
    tenant_id: uuid.UUID,
    name: str,
    ma_agent_id: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> dict[str, Any]:
    """Build an MA agent payload for use in httpx.MockTransport responses."""
    now = datetime.now(UTC).isoformat()
    agent_id = (
        ma_agent_id or f"agent_{secrets.token_urlsafe(18).replace('-', '').replace('_', '')[:24]}"
    )
    return {
        "id": agent_id,
        "type": "agent",
        "name": name,
        "version": 1,
        "model": {"id": model, "speed": "standard"},
        "system": None,
        "metadata": {
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: name,
        },
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "description": None,
    }


# ---------------------------------------------------------------------------
# (a) Names-only secret hygiene (the security invariant pyright cannot check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_section_data_secrets_returns_key_names_only_and_value_is_absent(
    db_session: AsyncSession,
) -> None:
    """secrets section must return key names only — values never leave the read layer."""
    tenant_id = await _seed_tenant(db_session)
    _account_id = await _seed_account(db_session, tenant_id)

    # Derive the agent_uuid the read layer uses for agent_files lookup
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID)

    # Seed a secret with a known VALUE that must not appear in the result
    secret_key = "API_TOKEN"
    secret_value = "s3cr3t-value-should-never-appear"
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        key=secret_key,
        content=secret_value,
    )

    # Build a fake MA handler that returns exactly one agent for this tenant
    agent_payload = _make_agent_payload(
        tenant_id=tenant_id, name=_AGENT_NAME, ma_agent_id=_MA_AGENT_ID
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(
                200, json={"data": [agent_payload], "has_more": False, "next_page": None}
            )
        return httpx.Response(404, json={"error": "unhandled"})

    anthropic = build_fake_anthropic(handler)

    result = await load_section_data(
        db_session,
        anthropic,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        section="secrets",
    )

    # The result must be a list of strings (key names only)
    assert isinstance(result, list), (
        "secrets section must return list of key names only — values never leave the read layer"
    )
    secret_names: list[str] = result  # type: ignore[assignment]
    assert secret_key in secret_names, "secrets section must include the key name 'API_TOKEN'"

    # The secret VALUE must never appear anywhere in the result
    serialized = repr(result)
    assert secret_value not in serialized, (
        "secrets section must return key names only — values never leave the read layer"
    )

    # Verify there is no 'value' or 'val' field on the result items (result is list[str])
    for item in secret_names:
        assert isinstance(item, str), (
            "secrets section items must be plain strings (key names), not objects with a value field"
        )

    # Negative check: the result should not contain the value even after json serialization
    json_serialized = json.dumps(secret_names)
    assert secret_value not in json_serialized, (
        "secrets section values must not appear in any serialized form of the result"
    )


# ---------------------------------------------------------------------------
# (b) Roster cap-25 over_cap count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_tenant_roster_caps_at_25_and_over_cap_count_is_correct(
    db_session: AsyncSession,
) -> None:
    """static_select caps at 25 options; the surplus surfaces as over_cap."""
    tenant_id = await _seed_tenant(db_session)

    # Build 28 agents tagged with this tenant
    num_agents = 28
    agents = [
        _make_agent_payload(tenant_id=tenant_id, name=f"agent-{i:03d}") for i in range(num_agents)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": agents, "has_more": False, "next_page": None})
        return httpx.Response(404, json={"error": "unhandled"})

    anthropic = build_fake_anthropic(handler)

    entries, over_cap = await load_tenant_roster(db_session, anthropic, tenant_id=tenant_id)

    assert len(entries) == 25, "static_select caps at 25 options; the surplus surfaces as over_cap"
    assert over_cap == 3, "over_cap must be the exact number of agents beyond the 25-option cap"


# ---------------------------------------------------------------------------
# (c) Scope-hint copy per UI-SPEC Copywriting Contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_scope_hint_returns_workspace_copy_when_tenant_scope_propagated(
    db_session: AsyncSession,
) -> None:
    """scope hint renders the UI-SPEC copy for each scope state."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)
    channel_id = "C_HINT_TEST"

    # Seed a workspace-scope propagation
    scope = TenantScopeRef(tenant_id=tenant_id)
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        mode="agent",
        actor_account_id=account_id,
    )

    result = await load_scope_hint(
        db_session,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        channel_id=channel_id,
    )

    assert result == ":globe_with_meridians: Set for *Whole workspace*", (
        "scope hint renders the UI-SPEC copy for each scope state"
    )


@pytest.mark.asyncio
async def test_load_scope_hint_returns_unset_copy_when_no_propagation_seeded(
    db_session: AsyncSession,
) -> None:
    """scope hint renders the unset copy when no propagation has been seeded."""
    tenant_id = await _seed_tenant(db_session)
    channel_id = "C_HINT_UNSET"

    result = await load_scope_hint(
        db_session,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        channel_id=channel_id,
    )

    assert result == "_(no default set for this agent)_", (
        "scope hint renders the UI-SPEC copy for each scope state"
    )

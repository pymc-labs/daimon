from __future__ import annotations

import uuid

from daimon.core.defaults.metadata import (
    find_conflicting_skill_body,
    strip_tenant_prefix,
    tenant_scoped_display_title,
)

# ---------------------------------------------------------------------------
# tenant_scoped_display_title tests
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.UUID("0b8b5903-1234-5678-9abc-def012345678")
_T8 = "0b8b5903"  # str(_TENANT_ID)[:8]


# ---------------------------------------------------------------------------
# strip_tenant_prefix tests
# ---------------------------------------------------------------------------


def test_strip_tenant_prefix_returns_none_for_other_tenant() -> None:
    other_tenant = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    title = tenant_scoped_display_title(tenant_id=other_tenant, name="cli-auth")
    result = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert result is None, "strip must return None for a title prefixed with a different tenant"


def _scoped_body(agent_name: str, name: str = "notes") -> str:
    title = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=name, agent_name=agent_name)
    body = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert body is not None
    return body


def test_find_conflicting_skill_body_scoped_create_ignores_other_agents_scoped() -> None:
    result = find_conflicting_skill_body(
        existing_bodies=["other agent/last30days"],
        name="last30days",
        agent_name="research daimon",
    )
    assert result is None, (
        "two agents may each own a scoped skill with the same name — they never mount together"
    )

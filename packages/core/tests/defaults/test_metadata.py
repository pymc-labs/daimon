from __future__ import annotations

import re
import uuid

from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
    build_metadata,
    strip_tenant_prefix,
    tenant_scoped_display_title,
)


def test_build_metadata_round_trip() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    md = build_metadata(tenant_id=tenant_id, name="daimon")
    assert md[MA_METADATA_KEY_TENANT] == str(tenant_id)
    assert md[MA_METADATA_KEY_NAME] == "daimon"
    assert len(md) == 2


def test_build_metadata_omits_account_when_none() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    md = build_metadata(tenant_id=tenant_id, name="daimon", account_id=None)
    assert MA_METADATA_KEY_ACCOUNT not in md, (
        "account_id=None must not stamp daimon_account — preserves seeded-default "
        "'everyone's agent' semantics"
    )
    assert set(md.keys()) == {MA_METADATA_KEY_TENANT, MA_METADATA_KEY_NAME}


def test_build_metadata_stamps_account_when_provided() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    account_id = uuid.uuid4()
    md = build_metadata(tenant_id=tenant_id, name="user-agent", account_id=account_id)
    assert md[MA_METADATA_KEY_ACCOUNT] == str(account_id), (
        "account_id=UUID must stamp daimon_account=str(UUID) for per-user roster filter"
    )
    assert md[MA_METADATA_KEY_TENANT] == str(tenant_id)
    assert md[MA_METADATA_KEY_NAME] == "user-agent"
    assert len(md) == 3


# ---------------------------------------------------------------------------
# tenant_scoped_display_title tests
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.UUID("0b8b5903-1234-5678-9abc-def012345678")
_T8 = "0b8b5903"  # str(_TENANT_ID)[:8]


def test_tenant_scoped_display_title_seeded_shape_short_name() -> None:
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name="cli-auth")
    assert result == f"{_T8}-cli-auth", (
        "seeded shape (agent_name=None) must return prefix+name verbatim when within limit"
    )


def test_tenant_scoped_display_title_seeded_shape_produces_prefix_dash_name() -> None:
    """seeded shape must produce f'{str(tenant_id)[:8]}-{name}' for short names"""
    name = "brainstorming"
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=name)
    assert result == f"{_T8}-{name}", (
        "seeded shape for a short name must be exactly prefix + '-' + name"
    )


def test_tenant_scoped_display_title_synced_shape() -> None:
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name="cli-auth", agent_name="daimon")
    assert result == f"{_T8}-daimon/cli-auth", (
        "synced shape (agent_name provided) must return prefix+agent_name/name"
    )


def test_tenant_scoped_display_title_boundary_63_chars_unmangled() -> None:
    # prefix is 9 chars ("0b8b5903-"), so body must be 54 to reach total 63
    body = "x" * 54
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body)
    assert len(result) == 63, "63-char title must not be mangled"
    assert result == f"{_T8}-{body}", "63-char title must be verbatim prefix+body"


def test_tenant_scoped_display_title_boundary_64_chars_unmangled() -> None:
    # body must be 55 to reach total 64
    body = "x" * 55
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body)
    assert len(result) == 64, "64-char title must not be mangled"
    assert result == f"{_T8}-{body}", "64-char title must be verbatim prefix+body"


def test_tenant_scoped_display_title_boundary_65_chars_mangled_to_64() -> None:
    # body must be 56 to reach total 65 → triggers mangling
    body = "x" * 56
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body)
    assert len(result) == 64, "mangled title must be exactly 64 chars"
    assert re.search(r"~[0-9a-f]{4}$", result), "mangled title must end with ~<4 hex chars>"


def test_tenant_scoped_display_title_hash_over_full_body_produces_distinct_titles() -> None:
    """Two long names sharing their first 50 chars must hash to different titles."""
    shared_prefix = "a" * 50
    body_a = shared_prefix + "b" * 10
    body_b = shared_prefix + "c" * 10
    result_a = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body_a)
    result_b = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body_b)
    assert result_a != result_b, (
        "hash is over full body so two long names sharing prefix must produce distinct titles"
    )
    # Both must be exactly 64 chars
    assert len(result_a) == 64
    assert len(result_b) == 64


def test_tenant_scoped_display_title_is_deterministic() -> None:
    kwargs = {"tenant_id": _TENANT_ID, "name": "my-skill", "agent_name": "daimon"}
    assert tenant_scoped_display_title(**kwargs) == tenant_scoped_display_title(**kwargs), (
        "same inputs must always produce identical output"
    )


# ---------------------------------------------------------------------------
# strip_tenant_prefix tests
# ---------------------------------------------------------------------------


def test_strip_tenant_prefix_round_trip_seeded() -> None:
    title = tenant_scoped_display_title(tenant_id=_TENANT_ID, name="cli-auth")
    body = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert body == "cli-auth", "strip must recover the body from a canonical seeded title"


def test_strip_tenant_prefix_round_trip_synced() -> None:
    title = tenant_scoped_display_title(tenant_id=_TENANT_ID, name="cli-auth", agent_name="daimon")
    body = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert body == "daimon/cli-auth", "strip must recover the body from a canonical synced title"


def test_strip_tenant_prefix_returns_none_for_other_tenant() -> None:
    other_tenant = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    title = tenant_scoped_display_title(tenant_id=other_tenant, name="cli-auth")
    result = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert result is None, "strip must return None for a title prefixed with a different tenant"


def test_strip_tenant_prefix_returns_none_for_unprefixed_title() -> None:
    result = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title="cli-auth")
    assert result is None, "strip must return None for a bare title without any prefix"


def test_strip_tenant_prefix_mangle_stability() -> None:
    """For a mangled title, strip then re-prefix must reproduce the same <=64-char string."""
    body = "x" * 60  # long enough to trigger mangling
    title = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body)
    assert len(title) == 64, "title should be mangled"
    stripped = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert stripped is not None, "strip must recognize own tenant's mangled title"
    prefix = f"{_T8}-"
    re_prefixed = f"{prefix}{stripped}"
    assert re_prefixed == title, (
        "re-prefixing a stripped mangled title must reproduce the same 64-char string — no re-mangle"
    )

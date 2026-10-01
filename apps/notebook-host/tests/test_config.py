"""DAIMON_NOTEBOOK__TENANTS parses every form a raw env_file line takes."""

from __future__ import annotations

from uuid import UUID

import pytest
from notebook_host.config import load_settings
from pydantic import ValidationError

_T1 = "6f1c2a3e-0b4d-4e5f-8a9b-0c1d2e3f4a5b"
_T2 = "7a2b3c4d-1e5f-4a6b-9c8d-1e2f3a4b5c6d"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("  ", ()),
        (_T1, (UUID(_T1),)),
        (f"{_T1}, {_T2.upper()}", (UUID(_T1), UUID(_T2))),
        (f"{_T1},", (UUID(_T1),)),
        (f'["{_T1}", "{_T2}"]', (UUID(_T1), UUID(_T2))),
        ("[]", ()),
    ],
)
def test_tenants_env_parses_when_empty_csv_or_json(
    raw: str, expected: tuple[UUID, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__TENANTS", raw)
    assert load_settings(_env_file=None).tenants == expected, f"{raw!r} should parse"


@pytest.mark.parametrize("raw", [f"{_T1},not-a-uuid", '["not-a-uuid"]', f'["{_T1}"'])
def test_tenants_env_fails_naming_the_variable_when_malformed(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("DAIMON_NOTEBOOK__TENANTS", raw)
    with pytest.raises(ValidationError, match="DAIMON_NOTEBOOK__TENANTS"):
        load_settings(_env_file=None)

"""Tests for report_host.config — never imports daimon."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "primary,backup")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "https://seam.example.com/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "https://reports.example.com")


def test_admin_secrets_csv_parses_into_list_of_two(monkeypatch: pytest.MonkeyPatch) -> None:
    from report_host.config import load_settings

    _set_required(monkeypatch)
    settings = load_settings(_env_file=None)
    values = [s.get_secret_value() for s in settings.admin_secrets]
    assert values == ["primary", "backup"], "CSV admin secrets should parse into a two-item list"


def test_missing_admin_secrets_raises_validation_error_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from report_host.config import load_settings

    monkeypatch.delenv("DAIMON_REPORT__ADMIN_SECRETS", raising=False)
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "https://seam.example.com/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "https://reports.example.com")

    with pytest.raises(ValidationError, match="DAIMON_REPORT__ADMIN_SECRETS"):
        load_settings(_env_file=None)


def test_missing_mcp_url_raises_rather_than_defaulting(monkeypatch: pytest.MonkeyPatch) -> None:
    from report_host.config import load_settings

    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "primary")
    monkeypatch.delenv("DAIMON_REPORT__MCP_URL", raising=False)
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "https://reports.example.com")

    with pytest.raises(ValidationError):
        load_settings(_env_file=None)


def test_missing_public_url_base_raises_rather_than_defaulting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from report_host.config import load_settings

    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "primary")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "https://seam.example.com/mcp")
    monkeypatch.delenv("DAIMON_REPORT__PUBLIC_URL_BASE", raising=False)

    with pytest.raises(ValidationError):
        load_settings(_env_file=None)


def test_reserve_usd_is_decimal_and_equals_default_from_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from report_host.config import load_settings

    _set_required(monkeypatch)
    settings = load_settings(_env_file=None)
    assert isinstance(settings.reserve_usd, Decimal), "reserve_usd must be a Decimal, not a float"
    assert settings.reserve_usd == Decimal("0.60"), "default reserve should be exactly $0.60"


def test_cap_and_interval_defaults_match_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    from report_host.config import load_settings

    _set_required(monkeypatch)
    settings = load_settings(_env_file=None)
    assert settings.max_pdf_bytes == 50 * 1024 * 1024
    assert settings.max_bundle_bytes == 25 * 1024 * 1024
    assert settings.max_open_threads_per_recipient == 3
    assert settings.max_running_turns_per_report == 4
    assert settings.poll_interval_seconds == 2.0
    assert settings.turn_timeout_seconds == 1200

from __future__ import annotations

from daimon.core._models import (
    Base,
)

PHASE_1_TABLES = {
    "tenants",
    "accounts",
    "cli_principals",
    "platform_principals",
    "principal_links",
    "user_config",
    "channel_config",
    "tenant_config",
}


def test_metadata_contains_all_phase_1_tables_when_imported() -> None:
    actual = set(Base.metadata.tables.keys())
    missing = PHASE_1_TABLES - actual
    assert not missing, f"Base.metadata is missing phase-1 tables: {missing}"

"""Pure safety and scheduling checks for the staging rehearsal script."""

from decimal import Decimal

from scripts.hackathon_load_rehearsal import arrival_offsets, budget_allows, staging_guard


def test_arrival_offsets_cover_window_without_delaying_first_tenant() -> None:
    assert arrival_offsets(4, 1800) == (0, 450, 900, 1350)
    assert arrival_offsets(3, 0) == (0, 0, 0)


def test_budget_stops_at_limit() -> None:
    limit = Decimal("5")
    assert budget_allows(Decimal("4.999999"), limit)
    assert not budget_allows(limit, limit)
    assert not budget_allows(Decimal("5.01"), limit)


def test_staging_requires_acknowledgement_and_real_marker() -> None:
    staging = "staging-daimon-mcp-123.us-east4.run.app"
    assert staging_guard(
        acknowledged=True, mcp_host=staging, marker_exists=True, marker_is_synthetic=False
    )
    assert not staging_guard(
        acknowledged=False, mcp_host=staging, marker_exists=True, marker_is_synthetic=False
    )
    assert not staging_guard(
        acknowledged=True, mcp_host=staging, marker_exists=False, marker_is_synthetic=False
    )
    assert not staging_guard(
        acknowledged=True, mcp_host=staging, marker_exists=True, marker_is_synthetic=True
    )


def test_staging_refuses_production_mcp_host_even_with_marker() -> None:
    assert not staging_guard(
        acknowledged=True,
        mcp_host="daimon-mcp.decision.ai",
        marker_exists=True,
        marker_is_synthetic=False,
    )
    assert not staging_guard(
        acknowledged=True, mcp_host=None, marker_exists=True, marker_is_synthetic=False
    )

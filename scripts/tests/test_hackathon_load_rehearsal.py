"""Pure safety and scheduling checks for the staging rehearsal script."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import yaml
from daimon.core.constants import DEFAULT_AGENT_MODEL

from scripts.hackathon_load_rehearsal import (
    _defaults_root,  # pyright: ignore[reportPrivateUsage]
    arrival_offsets,
    budget_allows,
    expected_agent_model,
    staging_guard,
)

if TYPE_CHECKING:
    from daimon.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_haiku_rehearsal_overrides_the_seeded_default_model(tmp_path: Path) -> None:
    settings = cast("Settings", SimpleNamespace(defaults_root=REPO_ROOT / "defaults"))
    root = _defaults_root(settings, "haiku", tmp_path)
    spec = yaml.safe_load((root / "agents" / "daimon.yaml").read_text())
    assert spec["model"] == "claude-haiku-4-5"
    assert expected_agent_model("haiku") == "claude-haiku-4-5"


def test_event_rehearsal_expects_the_seeded_default_model(tmp_path: Path) -> None:
    settings = cast("Settings", SimpleNamespace(defaults_root=REPO_ROOT / "defaults"))
    assert _defaults_root(settings, "event", tmp_path) == REPO_ROOT / "defaults"
    seeded = yaml.safe_load((REPO_ROOT / "defaults" / "agents" / "daimon.yaml").read_text())
    assert expected_agent_model("event") == seeded["model"] == DEFAULT_AGENT_MODEL

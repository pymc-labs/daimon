"""Pure safety and scheduling checks for the staging rehearsal script."""

from argparse import Namespace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest
import yaml
from daimon.core.constants import DEFAULT_AGENT_MODEL

from scripts.hackathon_load_rehearsal import (
    DiscordREST,
    _defaults_root,  # pyright: ignore[reportPrivateUsage]
    _discord_phase,  # pyright: ignore[reportPrivateUsage]
    _message_time,  # pyright: ignore[reportPrivateUsage]
    _percentile,  # pyright: ignore[reportPrivateUsage]
    _require_discord_model,  # pyright: ignore[reportPrivateUsage]
    arrival_offsets,
    budget_allows,
    expected_agent_model,
    round_robin_threads,
    staging_guard,
)

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from daimon.core.config import Settings
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_arrival_offsets_cover_window_without_delaying_first_tenant() -> None:
    assert arrival_offsets(4, 1800) == (0, 450, 900, 1350)
    assert arrival_offsets(3, 0) == (0, 0, 0)


def test_budget_stops_at_limit() -> None:
    limit = Decimal("5")
    assert budget_allows(Decimal("4.999999"), limit)
    assert not budget_allows(limit, limit)
    assert not budget_allows(Decimal("5.01"), limit)


def test_layout_turns_reach_every_team_before_second_thread() -> None:
    teams = [[f"{team}-{slot}" for slot in range(3)] for team in range(65)]
    ordered = round_robin_threads(teams)
    assert len(ordered) == 195
    assert {thread.split("-")[0] for thread in ordered[:100]} == {str(team) for team in range(65)}
    assert ordered[65] == "0-1"


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


def test_discord_percentiles_handle_empty_and_small_samples() -> None:
    assert _percentile([], 0.95) == "n/a"
    assert _percentile([1, 3, 5], 0.5) == "3.00"
    assert _percentile([1, 3, 5], 0.95) == "4.80"


def test_discord_final_uses_edit_time_and_first_uses_creation_time() -> None:
    started = datetime(2026, 10, 1, tzinfo=UTC)
    message: dict[str, object] = {
        "timestamp": (started + timedelta(seconds=2)).isoformat(),
        "edited_timestamp": (started + timedelta(seconds=15)).isoformat(),
    }
    assert _message_time(message, started) == 2
    assert _message_time(message, started, latest=True) == 15
    message["edited_timestamp"] = None
    assert _message_time(message, started, latest=True) == 2


def test_discord_model_requirement_rejects_staging_mismatch() -> None:
    _require_discord_model("claude-sonnet-5-5", None)
    _require_discord_model("claude-haiku-4-5", "claude-haiku-4-5")
    with pytest.raises(RuntimeError, match="claude-sonnet-5-5 != claude-haiku-4-5"):
        _require_discord_model("claude-sonnet-5-5", "claude-haiku-4-5")


@pytest.mark.asyncio
async def test_discord_refuses_non_qa_guild_before_reading_token() -> None:
    args = Namespace(discord_guild_id=["1533730917854609528"])
    with pytest.raises(RuntimeError, match="allow-list"):
        await _discord_phase(
            args,
            cast("AsyncAnthropic", None),
            cast("async_sessionmaker[AsyncSession]", None),
            cast("Settings", None),
        )


@pytest.mark.asyncio
async def test_discord_rest_retries_429_after_retry_after() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"retry_after": 0})
        return httpx.Response(200, json={"id": "ok"})

    rest = DiscordREST("test-token")
    await rest.client.aclose()
    rest.client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://discord.test"
    )
    try:
        assert await rest.request("GET", "/users/@me") == {"id": "ok"}
        assert calls == 2
    finally:
        await rest.close()

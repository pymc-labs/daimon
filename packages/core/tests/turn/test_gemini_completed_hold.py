"""A completed Gemini turn settles its cap hold to the measured cost, so `left` never drifts."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from daimon.core.turn.gemini import PROFILE, GeminiUsageRuntime
from daimon.core.turn.gemini_state import SQLiteFlashAccounting, outstanding_holds

from .test_gemini_termination import NativeHost, run
from .test_gemini_termination import clean_sdk_settings as clean_sdk_settings
from .test_gemini_termination import native_host as native_host
from .test_gemini_termination import owned_database_preflight as owned_database_preflight

HOLD = Decimal("0.25")  # the code-default agent template's per-turn hold


def ledger(native: NativeHost, path: Path) -> SQLiteFlashAccounting:
    accounting = SQLiteFlashAccounting(path, namespace="n", hold_usd=HOLD, cap_usd=Decimal("0.50"))
    runtime = native.deps.turn_runtimes[PROFILE]
    assert isinstance(runtime.usage_store, GeminiUsageRuntime)
    usage = replace(runtime.usage_store, accounting=accounting)
    native.deps = replace(native.deps, turn_runtimes={PROFILE: replace(runtime, usage_store=usage)})
    return accounting


def rows(path: Path) -> list[dict]:
    import sqlite3

    with sqlite3.connect(path) as db:
        (body,) = db.execute("SELECT body FROM payloads WHERE namespace=?", ("n",)).fetchone()
    return list(json.loads(body).values())


@pytest.mark.parametrize("turns", [1, 3])
async def test_every_completed_turn_settles_its_hold(
    native_host: NativeHost, tmp_path: Path, turns: int
) -> None:
    path = tmp_path / "holds.sqlite3"
    ledger(native_host, path)
    native_host.wire.status = "completed"
    for index in range(turns):
        outcome = await run(native_host, operation_key=f"turn-{index}")
        assert outcome.state.termination is not None
        assert not outcome.state.termination.is_failure
    held = rows(path)
    assert len(held) == turns
    # Each hold went back to zero against a measured cost, none left for the TTL.
    for row in held:
        assert row["outcome"] == "accepted" and row["held_usd"] == "0"
        assert row["settled_usd"] == str(HOLD) and Decimal(row["actual_usd"]) > 0
    summary = outstanding_holds(json.dumps({str(i): row for i, row in enumerate(held)}))
    assert summary["held_usd"] == "0" and summary["open_holds"] == 0


async def test_an_interaction_that_stops_for_a_tool_call_settles_too(
    native_host: NativeHost, tmp_path: Path
) -> None:
    # Its tool results continue as a new interaction, so this one's usage is final.
    path = tmp_path / "holds.sqlite3"
    ledger(native_host, path)
    native_host.wire.status = "requires_action"
    await run(native_host)
    (row,) = rows(path)
    assert row["held_usd"] == "0" and row["settled_usd"] == str(HOLD)

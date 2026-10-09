"""Production MA calls may only decrease outside the mux driver boundary."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAX_OUTSIDE_DRIVERS = 30
SPEC = importlib.util.spec_from_file_location("ma_inventory", ROOT / "scripts/ma_inventory.py")
assert SPEC is not None and SPEC.loader is not None
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def test_ma_call_ratchet() -> None:
    current = inventory.scan(ROOT)
    assert not current["parse_errors"], f"MA inventory has parse errors: {current['parse_errors']}"
    baseline = json.loads(inventory.BASELINE.read_text())
    assert not baseline["parse_errors"]
    ceiling = len(inventory.outside_driver_calls(baseline))
    assert ceiling <= MAX_OUTSIDE_DRIVERS, (
        f"Checked-in MA baseline grew above its fixed ceiling: {ceiling} > {MAX_OUTSIDE_DRIVERS}. "
        "Regenerating JSON must never raise the ceiling. Lower the literal alongside extraction."
    )
    calls = inventory.outside_driver_calls(current)
    assert len(calls) <= ceiling, (
        f"Production MA calls outside packages/mux/mux/drivers grew: {len(calls)} > {ceiling}. "
        "Move calls into a driver. After removing calls, regenerate and commit the LOWER baseline "
        "with: uv run python scripts/ma_inventory.py --output scripts/ma_inventory.json. "
        "Never raise the count.\n"
        + "\n".join(f"{call['file']}:{call['line']} {call['resource']}" for call in calls)
    )


def test_inventory_boundaries(tmp_path: Path) -> None:
    paths = {
        "packages/core/daimon/core/turn.py": "client.beta.sessions.events.list(session_id)",
        "packages/mux/mux/drivers/anthropic/turn.py": "client.beta.sessions.create()",
        "packages/core/tests/test_turn.py": "client.beta.sessions.create()",
        "packages/testing/daimon/testing/ma.py": "client.beta.agents.create()",
        "scripts/probe.py": "client.beta.environments.create()",
        "packages/core/daimon/core/thread_naming.py": "client.messages.create()",
        ".venv/vendor.py": "client.beta.sessions.create()",
        "defaults/plugin-seed/vendor.py": "client.beta.sessions.create()",
    }
    for name, source in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source + "\n")
    result = inventory.scan(tmp_path)
    assert result["files_scanned"] == 6
    assert len(result["calls"]) == 5
    assert [call["file"] for call in inventory.outside_driver_calls(result)] == [
        "packages/core/daimon/core/turn.py"
    ]
    assert inventory.serialize(result) == inventory.serialize(inventory.scan(tmp_path))


def test_inventory_reports_unparseable_source(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("async def broken(:\n")
    result = inventory.scan(tmp_path)
    assert result["parse_errors"][0][0] == "broken.py"


@pytest.mark.parametrize("resource", sorted(inventory.RESOURCES))
def test_inventory_managed_agent_resources(tmp_path: Path, resource: str) -> None:
    (tmp_path / "caller.py").write_text(f"await client.beta.{resource}.retrieve('id')\n")
    result = inventory.scan(tmp_path)
    assert len(inventory.outside_driver_calls(result)) == 1

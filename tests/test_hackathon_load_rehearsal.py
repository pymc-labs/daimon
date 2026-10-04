"""Guard the staging load driver's bounded and paced mechanics."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/hackathon_load_rehearsal.py"
spec = importlib.util.spec_from_file_location("hackathon_load_rehearsal", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)

sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_realistic_prompts_require_code_and_cover_three_tasks() -> None:
    assert len(module.REALISTIC_PROMPTS) == 3
    assert all("synthetic" in prompt.lower() for prompt in module.REALISTIC_PROMPTS)
    assert "pandas" in module.REALISTIC_PROMPTS[0]
    assert "matplotlib" in module.REALISTIC_PROMPTS[1]
    assert "PyMC" in module.REALISTIC_PROMPTS[2]


def test_reuse_fills_200_slots_from_195_threads() -> None:
    slots = list(range(195))
    result = module.reuse_thread_slots(slots, 200)
    assert len(result) == 200
    assert result[:195] == slots
    assert result[195:] == slots[:5]
    with pytest.raises(ValueError, match="more than once"):
        module.reuse_thread_slots(slots, 391)


def test_peak_tokens_uses_rolling_minute() -> None:
    start = datetime.now(UTC)
    assert (
        module._peak_tokens_per_minute(
            [
                (start, 100),
                (start + timedelta(seconds=59), 200),
                (start + timedelta(seconds=60), 400),
            ]
        )
        == 600
    )


@pytest.mark.asyncio
async def test_anthropic_remaining_tracks_minimum() -> None:
    values = iter(("8", "3", "5"))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={},
            request=request,
            headers={"anthropic-ratelimit-requests-remaining": next(values)},
        )

    transport = module.CountingTransport(httpx.MockTransport(respond))
    async with httpx.AsyncClient(transport=transport) as client:
        for _ in range(3):
            await client.get("https://example.test/v1/messages")
    assert transport.rate_limit_min == {"anthropic-ratelimit-requests-remaining": 3}

"""Current SDK wire requests remain explicit and old omitted-tools tapes refuse."""

import asyncio
from pathlib import Path

import pytest
from daimon.testing.provider_fixtures import load_provider_fixtures, turn_tape
from daimon.testing.provider_replay import provider_replay
from mux.drivers.gemini.transport import SDKTransport
from mux.errors import ProviderError

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "packages/testing/fixtures/target53"


@pytest.mark.parametrize("stale", [False, True])
async def test_current_request_matches_only_the_explicit_tools_tape(stale: bool) -> None:
    pack = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")
    scenario, fixture = pack.select("QA-NEW37-MARKDOWN-TABLE-READABLE", "gemini")
    turn = fixture.turns[0]
    tape = turn_tape(scenario, fixture, turn, key="qa-send")
    send = tape.replies[0]
    if stale:
        request = dict(turn.request)
        del request["tools"]
        send = send.model_copy(update={"request_json": request})
    tape = tape.model_copy(update={"replies": (send,)})
    async with provider_replay(
        tape,
        backend="gemini",
        profile=fixture.profile,
        model=fixture.model,
        source_root=DATA / "catalog",
    ) as replay:
        assert isinstance(replay.transport, SDKTransport)
        if stale:
            with pytest.raises((AssertionError, ProviderError)):
                await asyncio.wait_for(replay.transport.create(turn.request), timeout=5)
            assert replay.wire.violations
        else:
            created = await asyncio.wait_for(replay.transport.create(turn.request), timeout=5)
            assert created["id"] == turn.root_id
            replay.wire.assert_consumed()
        assert len(replay.wire.requests) == 1
        body = replay.wire.requests[0].json()
        assert isinstance(body, dict) and body["tools"] == []

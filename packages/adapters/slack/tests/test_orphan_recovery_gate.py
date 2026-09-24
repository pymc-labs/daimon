"""The first Slack turn waits until boot recovery has captured old markers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.slack.app import SlackApp
from sqlalchemy.exc import OperationalError


async def test_turn_admission_waits_for_single_orphan_recovery_task() -> None:
    runtime = MagicMock()
    app = SlackApp(runtime=runtime)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _recover(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    sweep = AsyncMock(side_effect=_recover)
    with patch("daimon.adapters.slack.app.retire_orphaned_turns", new=sweep):
        first = app.start_orphan_recovery()
        second = app.start_orphan_recovery()
        waiter = asyncio.create_task(app._wait_for_orphan_recovery())  # pyright: ignore[reportPrivateUsage]
        await entered.wait()

        assert first is second, "repeated startup calls must not take a second marker snapshot"
        assert not waiter.done(), "turn admission must stay behind recovery"

        release.set()
        await waiter
        await first

    assert sweep.await_count == 1


async def test_transient_orphan_recovery_failure_retries_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    runtime = MagicMock()
    runtime.sessionmaker = MagicMock(side_effect=_Session)
    app = SlackApp(runtime=runtime)
    entered = asyncio.Event()

    async def _recover(*_args: object, **_kwargs: object) -> None:
        if sweep.await_count == 1:
            entered.set()
            raise OperationalError("SELECT", {}, RuntimeError("temporary DB outage"))
        entered.set()

    sweep = AsyncMock(side_effect=_recover)
    app._orchestrate = AsyncMock()  # type: ignore[method-assign]
    # Keep the retry path deterministic and fast while preserving its await.
    monkeypatch.setattr("daimon.adapters.slack.app._ORPHAN_RECOVERY_RETRY_DELAY_S", 0.001)
    with (
        patch("daimon.adapters.slack.app.retire_orphaned_turns", new=sweep),
        patch("daimon.adapters.slack.app.insert_if_new", new=AsyncMock(return_value=True)),
        patch(
            "daimon.adapters.slack.app.get_slack_bot_token",
            new=AsyncMock(return_value=SimpleNamespace(encrypted_token="ciphertext")),
        ),
        patch("daimon.adapters.slack.app.build_multifernet", return_value=object()),
        patch("daimon.adapters.slack.app.decrypt_token", return_value="xoxb-test"),
        patch("daimon.adapters.slack.app.AsyncWebClient", return_value=MagicMock()),
        patch("daimon.adapters.slack.app.is_slack_connect_external", return_value=False),
    ):
        recovery = app.start_orphan_recovery()
        await entered.wait()
        await app._handle_app_mention(  # pyright: ignore[reportPrivateUsage]
            {"channel": "C_TEST", "event_ts": "1000000001.000001"}, team_id="T_TEST"
        )

    assert recovery.done() and recovery.exception() is None
    assert sweep.await_count == 2
    app._orchestrate.assert_awaited_once()

"""Slack admission count drives periodic render cadence."""

import uuid
from unittest.mock import MagicMock

from daimon.adapters.slack.app import SlackApp
from daimon.core.config import TurnRenderSettings


def test_workspace_turn_claim_stretches_and_release_restores_interval() -> None:
    runtime = MagicMock()
    runtime.settings.turn_render = TurnRenderSettings(stretch_threshold=1, stretched_interval_s=4.0)
    app = SlackApp(runtime=runtime)
    first = uuid.uuid4()
    second = uuid.uuid4()

    app._claim_inflight(first, 0)  # pyright: ignore[reportPrivateUsage]
    assert app._render_interval.current() == 2.0  # pyright: ignore[reportPrivateUsage]
    app._claim_inflight(second, 0)  # pyright: ignore[reportPrivateUsage]
    assert app._render_interval.current() == 4.0  # pyright: ignore[reportPrivateUsage]
    app._release_inflight(first)  # pyright: ignore[reportPrivateUsage]
    assert app._render_interval.current() == 2.0  # pyright: ignore[reportPrivateUsage]
    app._release_inflight(second)  # pyright: ignore[reportPrivateUsage]

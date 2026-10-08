"""Adaptive periodic card-edit cadence."""

from unittest.mock import patch

import pytest
from daimon.core.config import TurnRenderSettings
from daimon.core.turn.render_interval import AdaptiveRenderInterval
from pydantic import ValidationError


def test_interval_stretches_once_above_threshold_and_restores_at_threshold() -> None:
    interval = AdaptiveRenderInterval(platform="discord", threshold=50, stretched_interval_s=5.0)
    with patch("daimon.core.turn.render_interval.log") as log:
        interval.observe(50)
        assert interval.current() == 2.0
        interval.observe(51)
        interval.observe(200)
        assert interval.current() == 5.0
        interval.observe(50)
        interval.observe(0)
        assert interval.current() == 2.0

    assert [call.args[0] for call in log.info.call_args_list] == [
        "turn.render_interval_stretched",
        "turn.render_interval_restored",
    ]
    assert [call.kwargs["in_flight"] for call in log.info.call_args_list] == [51, 50]


@pytest.mark.parametrize("seconds", [4.0, 5.0, 6.0])
def test_config_accepts_stretched_interval_range(seconds: float) -> None:
    assert TurnRenderSettings(stretched_interval_s=seconds).stretched_interval_s == seconds


@pytest.mark.parametrize("seconds", [3.9, 6.1])
def test_config_rejects_interval_outside_range(seconds: float) -> None:
    with pytest.raises(ValidationError):
        TurnRenderSettings(stretched_interval_s=seconds)

"""Process-local interval for periodic turn-card edits."""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

NORMAL_INTERVAL_S = 2.0


class AdaptiveRenderInterval:
    def __init__(self, *, platform: str, threshold: int, stretched_interval_s: float) -> None:
        self.platform = platform
        self.threshold = threshold
        self.stretched_interval_s = stretched_interval_s
        self._stretched = False

    def observe(self, in_flight: int) -> None:
        stretched = in_flight > self.threshold
        if stretched == self._stretched:
            return
        self._stretched = stretched
        log.info(
            "turn.render_interval_stretched" if stretched else "turn.render_interval_restored",
            platform=self.platform,
            in_flight=in_flight,
            interval_s=self.current(),
        )

    def current(self) -> float:
        return self.stretched_interval_s if self._stretched else NORMAL_INTERVAL_S

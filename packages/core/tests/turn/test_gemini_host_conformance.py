"""Gemini host wake/pump controls, isolated from the other provider cut."""

from types import SimpleNamespace

import pytest
from daimon.core.turn.gemini import GeminiTurnIO

from .native_conformance_support import (
    test_c17_real_wake_takeover_binding_generation_and_driver_lease as test_c17_real_wake_takeover_binding_generation_and_driver_lease,
)
from .native_conformance_support import (
    test_collector_joins_actual_host_pump_and_prepared_observation as test_collector_joins_actual_host_pump_and_prepared_observation,
)
from .test_conformance import SCOPE, session
from .test_conformance import OfflineIO as HostIO
from .test_conformance import host as host
from .test_conformance import run_host as drive_host


class OfflineIO(HostIO):
    def __init__(self, event, persistence=None):
        super().__init__(
            event,
            persistence,
            frame=GeminiTurnIO(
                SimpleNamespace(
                    capabilities=lambda: SimpleNamespace(profile_id="gemini.inline_reuse")
                ),
                SCOPE,
                session("gemini"),
            ).frame(event, {}),
        )


@pytest.fixture
def provider():
    return "gemini"


@pytest.fixture
def run_host():
    async def drive(monkeypatch, event, persistence=None):
        return await drive_host(monkeypatch, event, persistence, io_factory=OfflineIO)

    return drive

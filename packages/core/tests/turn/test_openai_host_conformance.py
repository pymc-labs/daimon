"""Openai host wake/pump controls, isolated from the other provider cut."""

import pytest
from daimon.core.turn.io import TurnEvent
from daimon.core.turn.openai_codec import display_event

from .native_conformance_support import (
    test_c17_real_wake_takeover_binding_generation_and_driver_lease as test_c17_real_wake_takeover_binding_generation_and_driver_lease,
)
from .native_conformance_support import (
    test_collector_joins_actual_host_pump_and_prepared_observation as test_collector_joins_actual_host_pump_and_prepared_observation,
)
from .test_conformance import OfflineIO as HostIO
from .test_conformance import host as host
from .test_conformance import run_host as drive_host


class OfflineIO(HostIO):
    def __init__(self, event, persistence=None):
        super().__init__(
            event,
            persistence,
            frame=TurnEvent(display_event(event, session_id=event.session_id), event),
        )


@pytest.fixture
def provider():
    return "openai"


@pytest.fixture
def run_host():
    async def drive(monkeypatch, event, persistence=None):
        return await drive_host(monkeypatch, event, persistence, io_factory=OfflineIO)

    return drive

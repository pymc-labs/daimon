"""Every Teams watch report rechecks that its owner is still in the conversation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams.app import TeamsApp
from daimon.core.turn.errors import AdmissionDenied

ROW = cast(Any, SimpleNamespace(requester_external_user_id="aad-1"))


async def _check(direct: object) -> AsyncMock:
    app = cast(Any, SimpleNamespace(_direct=direct, runtime=SimpleNamespace(sessionmaker=None)))
    pause = AsyncMock(return_value=AdmissionDenied(reason="invoker_not_allowed"))
    with patch("daimon.adapters.teams.app.pause_for_lost_access", pause):
        await TeamsApp._require_watch_owner(app, ROW, "conv")  # pyright: ignore[reportPrivateUsage]
    return pause


async def test_an_owner_still_in_the_roster_gets_the_report() -> None:
    pause = await _check(SimpleNamespace(member=AsyncMock(return_value="29:owner")))
    pause.assert_not_awaited()


async def test_an_owner_gone_from_the_roster_pauses_the_watch() -> None:
    with pytest.raises(AdmissionDenied):
        await _check(SimpleNamespace(member=AsyncMock(return_value=None)))


async def test_no_roster_reader_refuses_without_pausing() -> None:
    with pytest.raises(AdmissionDenied):
        await _check(None)

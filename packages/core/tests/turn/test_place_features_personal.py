"""A turn's personal Drive and calendar features go Off where the workspace has no Google source."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core.data_tools import DATA_SOURCES, PERSONAL_CALENDAR, PERSONAL_DRIVE
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot
from daimon.core.turn import admission


@pytest.mark.parametrize(
    ("any_source", "google", "data_on", "personal_on"),
    [(False, False, False, False), (True, False, True, False), (True, True, True, True)],
    ids=["no source", "no Google source", "a Google source"],
)
async def test_personal_features_need_a_google_source(
    monkeypatch: pytest.MonkeyPatch,
    any_source: bool,
    google: bool,
    data_on: bool,
    personal_on: bool,
) -> None:
    place = FeaturePlace(uuid.uuid4(), "discord")
    on = ResolvedFeature("k", "on", "default")
    found = FeatureSnapshot(
        place, 1, {k: on for k in (DATA_SOURCES, PERSONAL_DRIVE, PERSONAL_CALENDAR)}
    )

    async def resolved(*_: object, **__: object) -> FeatureSnapshot:
        return found

    async def has_sources(*_: object, google: bool = False) -> bool:
        return google_ok if google else any_source

    google_ok = google
    monkeypatch.setattr(admission, "resolve_features", resolved)
    monkeypatch.setattr(admission, "builtins_of", lambda _a: {})
    monkeypatch.setattr(admission, "has_sources", has_sources)
    deps = SimpleNamespace(sessionmaker=object(), settings=object())
    got = await admission.place_features(cast(Any, deps), place, cast(Any, object()))
    assert got.features[DATA_SOURCES].on is data_on
    assert got.features[PERSONAL_DRIVE].on is personal_on
    assert got.features[PERSONAL_CALENDAR].on is personal_on

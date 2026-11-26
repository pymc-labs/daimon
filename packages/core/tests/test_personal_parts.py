"""Which personal parts a workspace allows: each by its own feature, none without data sources."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core import personal_parts
from daimon.core.config import Settings


def _features(monkeypatch: pytest.MonkeyPatch, on: set[str]) -> None:
    async def feature_in(_s: object, _c: object, _p: object, key: str) -> Any:
        return SimpleNamespace(on=key in on)

    monkeypatch.setattr(personal_parts, "feature_in", feature_in)


async def _allowed() -> frozenset[str]:
    return await personal_parts.allowed_parts(
        cast(Any, None), cast(Settings, None), uuid.uuid4(), "discord"
    )


@pytest.mark.parametrize(
    ("on", "want"),
    [
        ({"data_sources", "data_personal_drive", "data_personal_calendar"}, {"drive", "calendar"}),
        ({"data_sources", "data_personal_calendar"}, {"calendar"}),
        ({"data_sources", "data_personal_drive"}, {"drive"}),
        ({"data_personal_drive", "data_personal_calendar"}, set[str]()),
    ],
    ids=["both", "drive off", "calendar off", "no data sources"],
)
async def test_each_part_follows_its_own_workspace_setting(
    monkeypatch: pytest.MonkeyPatch, on: set[str], want: set[str]
) -> None:
    _features(monkeypatch, on)
    assert await _allowed() == want

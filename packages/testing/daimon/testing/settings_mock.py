"""A `MagicMock` standing in for `Settings`, with a real `features` mapping so feature
resolution reads deployment values, not mocks."""

from __future__ import annotations

from unittest.mock import MagicMock


def mock_settings(**features: str) -> MagicMock:
    """`Settings` stand-in; `features` are the deployment tier's values (`DAIMON_FEATURES__*`)."""
    settings = MagicMock()
    settings.features = dict(features)
    return settings

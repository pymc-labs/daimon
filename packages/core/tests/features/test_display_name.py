import uuid

import pytest
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import feature_refusal
from daimon.core.features.snapshot import FeaturePlace


@pytest.mark.parametrize(
    ("value", "ok"),
    [("Research Assistant", True), ("id", True), ("x" * 41, False), ("a:b", False), (" x", False)],
)
def test_display_name_takes_one_short_line(value: str, ok: bool) -> None:
    assert FEATURES["display_name"].accepts(value, "agent") is ok


def test_a_bad_display_name_is_refused_with_the_rule() -> None:
    place = FeaturePlace(uuid.uuid4(), "discord", agent_id="agent_1")
    refused = feature_refusal(
        "display_name", "agent", "a:b", locked=False, place=place, settings=None
    )
    assert refused is not None and "40 characters" in str(refused)

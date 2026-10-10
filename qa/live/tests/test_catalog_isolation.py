from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from qa.live.schema import Assertion, ProposedScenario, Scenario, load_catalog


@pytest.mark.parametrize(
    "assertion",
    [
        {"kind": "channel_text_absent", "since_turn": 1, "channel": "{thread:P}", "pattern": "bad"},
        {"kind": "text_present", "turn": 1, "unexpected_argument": True},
    ],
)
def test_noncanary_bad_contract_does_not_block_catalog(
    tmp_path: Path, scenario: Scenario, assertion: dict[str, object]
) -> None:
    document = scenario.model_dump(mode="json", by_alias=True)
    document["tier"] = "full"
    document["assert"] = [assertion]
    (tmp_path / "pending.yaml").write_text(yaml.safe_dump(document))
    healthy = scenario.model_dump(mode="json", by_alias=True)
    healthy["id"] = "QA-HEALTHY"
    (tmp_path / "healthy.yaml").write_text(yaml.safe_dump(healthy))
    loaded = load_catalog(tmp_path)
    assert len(loaded) == 2
    assert any(isinstance(item, ProposedScenario) for item in loaded)
    document["tier"] = "canary"
    (tmp_path / "pending.yaml").write_text(yaml.safe_dump(document))
    if "unexpected_argument" in assertion:
        with pytest.raises(ValueError):
            load_catalog(tmp_path)


def test_since_turn_only_channel_assertions() -> None:
    with pytest.raises(ValidationError):
        Assertion(kind="text_present", since_turn=1, pattern="hello")
    with pytest.raises(ValidationError):
        Assertion(kind="channel_text_absent", turn=1, pattern="hello")

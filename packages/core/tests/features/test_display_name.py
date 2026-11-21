import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.agent_identity import shown_name
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.features import service
from daimon.core.features.registry import FEATURES
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.service import FeatureActor, feature_refusal
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


def test_shown_name_is_the_setting_or_the_fallback() -> None:
    assert shown_name(ResolvedFeature("display_name", "Ada", "agent"), "research") == "Ada"
    assert shown_name(ResolvedFeature("display_name", "id", "default"), "research") == "research"
    assert shown_name(None, "research") == "research"


@pytest.mark.parametrize("value", ["<!channel>", "@here", "#general", "**Ada**", "a_b", "Clyde"])
def test_display_name_refuses_mentions_markdown_and_platform_words(value: str) -> None:
    assert not FEATURES["display_name"].accepts(value, "agent")


def _actor(*, operator: bool = False) -> FeatureActor:
    async def names(_agent_id: str) -> list[str]:
        return ["research"]

    return FeatureActor(
        tenant_id=uuid.uuid4(),
        account_id=None,
        surface="chat",
        native_user_id=None,
        admin_for=AsyncMock(return_value=True),
        operator=operator,
        agent_names=names,
    )


@pytest.mark.parametrize(
    ("value", "operator", "taken"),
    [
        ("Daimon", False, True),
        ("daimon", True, False),
        ("Sales", False, True),
        ("Helper Bot", False, True),
        ("Research", False, False),
        ("Ada", False, False),
    ],
)
async def test_a_display_name_may_not_pass_for_daimon_or_another_agent(
    monkeypatch: pytest.MonkeyPatch, value: str, operator: bool, taken: bool
) -> None:
    row = SimpleNamespace(agent_id="agent_2", value="Helper Bot")
    monkeypatch.setattr(service, "avatar_names", AsyncMock(return_value=["research", "sales"]))
    monkeypatch.setattr(service, "list_feature_values", AsyncMock(return_value=[row]))
    sessions = MagicMock(return_value=AsyncMock())
    place = FeaturePlace(uuid.uuid4(), "discord", agent_id="agent_1")
    refused = service._refuse_taken_name(sessions, _actor(operator=operator), place, value)
    if taken:
        with pytest.raises(ChannelChangeRefused, match="another agent"):
            await refused
    else:
        await refused

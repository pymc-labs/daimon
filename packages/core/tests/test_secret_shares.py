"""The share rules shared by /share-secret and `share_secret_with`."""

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core import secret_share_flow as flow
from daimon.core import secret_shares as words


def test_a_label_is_one_tidy_line_of_at_most_100_characters() -> None:
    assert words.clean_label("  Prod   DB ") == "Prod DB"
    assert words.clean_label("a\nb") is None
    assert words.clean_label("   ") is None
    assert words.clean_label("x" * 101) is None


def test_a_value_fits_the_one_time_code_block() -> None:
    assert words.value_ok("s3cret")
    assert not words.value_ok("")
    assert not words.value_ok(" \n")
    assert not words.value_ok("a```b")
    assert not words.value_ok("x" * (words.MAX_VALUE + 1))


def test_a_label_cannot_mention_or_break_out_of_bold() -> None:
    text = words.card_text(
        sender="1", recipient="2", label="**x** <@3> @everyone", expires_at=datetime.now(UTC)
    )
    assert "<@3>" not in text and re.findall(r"<@(\d+)>", text) == ["2", "1"]
    assert "\\*\\*x\\*\\*" in text


@pytest.mark.parametrize(
    ("confirm", "template"), [(False, words.CUSTOM_ID_TEMPLATE), (True, words.CONFIRM_ID_TEMPLATE)]
)
def test_the_button_ids_round_trip_the_tenant_and_share(confirm: bool, template: str) -> None:
    tenant_id, share_id = uuid.uuid4(), uuid.uuid4()
    custom_id = words.build_custom_id(str(tenant_id), str(share_id), confirm=confirm)
    match = re.fullmatch(template, custom_id)
    assert match is not None and (match["tenant_id"], match["share_id"]) == (
        str(tenant_id),
        str(share_id),
    )
    assert len(custom_id) <= 100, "Discord's custom id limit"


@pytest.mark.parametrize(
    ("on", "dms", "allowed", "refusal"),
    [
        (False, True, True, words.OFF),
        (True, False, True, words.DMS_OFF),
        (True, True, False, words.DM_NOT_ALLOWED),
        (True, True, True, None),
    ],
)
async def test_sharing_needs_the_switch_for_the_agent_dms_on_and_the_dm_policy(
    monkeypatch: pytest.MonkeyPatch, on: bool, dms: bool, allowed: bool, refusal: str | None
) -> None:
    feature = AsyncMock(return_value=SimpleNamespace(on=on))
    monkeypatch.setattr(flow, "feature_in", feature)
    monkeypatch.setattr(flow, "dm_enabled", AsyncMock(return_value=dms))

    def allows(_recipient: str) -> bool:
        return allowed

    policy = MagicMock(return_value=SimpleNamespace(allows=allows))
    monkeypatch.setattr(flow, "env_dm_policy", policy)
    got = await flow.sharing_refusal(
        MagicMock(), MagicMock(), tenant_id=uuid.uuid4(), recipient="2", agent_id="ag_1"
    )
    assert got == refusal
    assert feature.await_args is not None and feature.await_args.args[2].agent_id == "ag_1"

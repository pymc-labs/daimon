"""`other_app_names`: a place that keeps other chat apps' names and links keeps them in the
turn, its card, routine posts and the prompt; off, every one is scrubbed as before."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from daimon.core.admin_actions import DelegatedAction
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.session_identity import IDENTITY_KEYS, SessionIdentity
from daimon.core.features.snapshot import (
    FeaturePlace,
    FeatureSnapshot,
    ResolvedFeature,
)
from daimon.core.platform_system import system_for_platform
from daimon.core.routine_delivery import (
    render_fallback_dm,
    render_fallback_post,
    render_routine_controls,
    routine_keeps_names,
)
from daimon.core.stores.domain import RoutineRow
from daimon.core.turn.admission import Admission
from daimon.core.turn.card_state import CardEvent, CardState, update, update_activity
from daimon.core.turn.other_apps import mark_turn, place_scrub_platform, scrub_platform
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "other_app_names"
SAID = "Thread: https://acme.slack.com/archives/C1/p1. Ask on Slack or Microsoft Teams."
SCRUBBED = "Thread: another app. Ask on another app."


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        }
    )


def _admin(tenant_id: uuid.UUID) -> FeatureActor:
    async def admin_for(_: DelegatedAction | None) -> bool:
        return True

    return FeatureActor(tenant_id, None, "web", None, admin_for)


async def _keep(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, platform: str = "discord"
) -> None:
    await change_feature(
        factory,
        _settings(),
        _admin(tenant_id),
        key=KEY,
        tier="workspace",
        value="on",
        place=FeaturePlace.of(tenant_id, platform, None),
    )


class _Stub:
    """Just the part of an `Admission` that `shown` reads."""

    def __init__(self, features: FeatureSnapshot) -> None:
        self.features = features

    def shown(self, key: str, platform: str = "") -> bool:
        return Admission.shown(cast("Any", self), key, platform)


def _admission(value: str | None) -> Admission:
    found = {} if value is None else {KEY: ResolvedFeature(KEY, value, "workspace")}
    snapshot = FeatureSnapshot(FeaturePlace(uuid.uuid4(), "discord"), 1, found)
    return cast("Admission", _Stub(snapshot))


def _row(**overrides: object) -> RoutineRow:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "created_by_user_id": "U1",
        "agent_id": "ag",
        "agent_name": "daimon",
        "cron_expr": "0 9 * * 1",
        "timezone": "UTC",
        "trigger_message": "go",
        "enabled": True,
        "next_fire_at": None,
        "last_fired_at": None,
        "last_error": None,
        "last_result_tail": SAID,
        "delivery_payload": SAID,
        "destination_kind": "channel",
        "destination_id": "C1",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return RoutineRow.model_validate(base)


# --- registry -----------------------------------------------------------------


def test_the_setting_is_off_by_default_and_follows_the_session_prompt() -> None:
    spec = FEATURES[KEY]
    assert (spec.default, spec.binds, spec.name) == ("off", "session_identity", "Name other apps")
    assert KEY in IDENTITY_KEYS


# --- turn and card -------------------------------------------------------------


@pytest.mark.parametrize(("value", "kept"), [("on", True), ("off", False), (None, False)])
def test_a_turn_marks_whether_it_keeps_names(value: str | None, kept: bool) -> None:
    observation = TurnObservation(cast("Any", None), None, "discord")
    with observation.activate():
        assert mark_turn(_admission(value), "discord") is kept
        assert observation.other_app_names is kept
        assert scrub_platform("discord") == (None if kept else "discord")
    assert scrub_platform("discord") == "discord", "outside a turn the names are scrubbed"


def _card_after(label: str, *, kept: bool) -> CardState:
    observation = TurnObservation(cast("Any", None), None, "teams")
    observation.other_app_names = kept
    with observation.activate():
        card = update(CardState(platform="teams"), CardEvent(kind="message", label=label))
        tool = ToolUseBlock(
            kind="tool_use",
            id="t1",
            type="agent.mcp_tool_use",
            name="Search_Slack_messages",
            input={},
            mcp_server_name="Slack",
        )
        return update_activity(card, TurnState(content=[tool]))


def test_a_card_keeps_the_draft_and_status_lines_when_the_turn_does() -> None:
    kept = _card_after("Asking Slack or Discord", kept=True)
    assert kept.text_preview == "Asking Slack or Discord"
    assert any("Slack" in line for line in kept.tool_lines)


def test_a_card_scrubs_the_draft_and_status_lines_otherwise() -> None:
    scrubbed = _card_after("Asking Slack or Discord", kept=False)
    assert scrubbed.text_preview == "Asking another app"
    assert all("Slack" not in line for line in scrubbed.tool_lines)


# --- routines ------------------------------------------------------------------


def test_a_routine_post_keeps_the_names_and_the_link_when_told_to() -> None:
    row = _row()
    kept = render_fallback_post(row, platform="discord", keep_names=True)
    assert SAID in kept
    assert SCRUBBED in render_fallback_post(row, platform="discord")
    assert SCRUBBED in render_fallback_dm(row, "gone", platform="discord"), "a DM always scrubs"


def test_a_routine_run_is_not_told_to_hide_other_apps_when_it_keeps_them() -> None:
    row = _row()
    off = render_routine_controls(row, platform="discord")
    on = render_routine_controls(row, platform="discord", keep_names=True)
    assert "Never name or hint at another chat app" in off
    assert "Never name or hint at another chat app" not in on
    assert "In Discord, Daimon works" in on


async def test_the_poster_reads_the_setting_at_the_routines_agent_and_destination(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    row = _row(tenant_id=tenant.id)
    assert not await routine_keeps_names(db_session_factory, _settings(), row, "discord")
    await _keep(db_session_factory, tenant.id)
    assert await routine_keeps_names(db_session_factory, _settings(), row, "discord")


async def test_a_failed_read_keeps_the_scrub() -> None:
    def broken() -> Any:
        raise RuntimeError("database is down")

    place = FeaturePlace.of(uuid.uuid4(), "discord", "C1")
    assert await place_scrub_platform(cast("Any", broken), _settings(), place) == "discord"


# --- the prompt ----------------------------------------------------------------

_PROMPT = "You are an agent.\nAsk on Slack when stuck.\nBe brief."


def test_the_prompt_drops_the_lines_naming_other_apps_unless_they_are_kept() -> None:
    dropped = system_for_platform(_PROMPT, "discord")
    assert dropped is not None and "Slack" not in dropped
    assert system_for_platform(_PROMPT, "discord", keep_other_names=True) is None, (
        "nothing to swap or drop, so the agent's own prompt stands"
    )


def test_the_session_identity_carries_the_setting_into_the_prompt() -> None:
    off = SessionIdentity.frozen([])
    on = SessionIdentity.frozen([(KEY, "on")])
    assert "Slack" not in (off.system(_PROMPT, "discord") or "")
    assert on.system(_PROMPT, "discord") is None
    assert on.stored() == ((KEY, "on"),)

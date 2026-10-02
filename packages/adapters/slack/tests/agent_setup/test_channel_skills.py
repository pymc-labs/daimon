"""The channel skills form's submission: what it reads, who may save, what it stores."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.slack.agent_setup import channel_skills
from daimon.adapters.slack.agent_setup.channel_skills import (
    ChannelSkillsSubmission,
    evaluate_channel_skills_submission,
    run_channel_skills_submission,
)
from daimon.adapters.slack.agent_setup.panel_views import (
    CHANNEL_SKILLS_ADD_INPUT_ID,
    CHANNEL_SKILLS_REMOVE_INPUT_ID,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEAM = "T0SKILLS"
CHANNEL = "C0GROWTH"
META = PanelMetadata(team_id=TEAM, channel_id=CHANNEL, view="channel_skills", root_view_id="V1")
_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC).isoformat()


def test_evaluate_reads_the_skill_to_add_and_the_ticked_ones() -> None:
    values: dict[str, Any] = {
        CHANNEL_SKILLS_ADD_INPUT_ID: {CHANNEL_SKILLS_ADD_INPUT_ID: {"value": " pdf-tools "}},
        CHANNEL_SKILLS_REMOVE_INPUT_ID: {
            CHANNEL_SKILLS_REMOVE_INPUT_ID: {"selected_options": [{"value": "skill_old"}]}
        },
    }
    payload = {
        "view": {"private_metadata": encode_panel_metadata(META), "state": {"values": values}}
    }
    assert evaluate_channel_skills_submission(payload) == ChannelSkillsSubmission(
        meta=META, add="pdf-tools", remove=("skill_old",)
    )
    assert evaluate_channel_skills_submission({"view": {}}) is None, "no panel metadata"


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> MagicMock:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/skills":
            skill = {
                "id": "skill_lib",
                "created_at": _NOW,
                "display_title": tenant_scoped_display_title(tenant_id=tenant_id, name="pdf-tools"),
                "latest_version": "v3",
                "source": "custom",
                "type": "skill",
                "updated_at": _NOW,
            }
            return httpx.Response(200, json={"data": [skill], "next_page": None})
        agent = ma_agent(name="shared", tenant_id=tenant_id).model_dump(mode="json")
        return httpx.Response(200, json={"data": [agent], "next_page": None})

    runtime = MagicMock()
    runtime.sessionmaker = sessionmaker
    runtime.anthropic = build_fake_anthropic(handler)
    runtime.deployment_default = DeploymentDefault(agent_name="shared")
    return runtime


async def _run(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    admin: bool,
    add: str = "",
    remove: tuple[str, ...] = (),
) -> MagicMock:
    monkeypatch.setattr(channel_skills, "resolve_is_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(channel_skills, "load_routing_view", AsyncMock(return_value={"v": 1}))
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.views_update = AsyncMock()
    await run_channel_skills_submission(
        _runtime(sessionmaker),
        client,
        team_id=TEAM,
        user_id="U0ADMIN",
        submission=ChannelSkillsSubmission(meta=META, add=add, remove=remove),
    )
    return client


async def test_admin_adds_then_removes_and_the_routing_view_refreshes(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM)
    async with db_session_factory.begin() as session:
        await make_tenant(session, platform="slack", workspace_id=TEAM)

    client = await _run(db_session_factory, monkeypatch, admin=True, add="pdf-tools")
    async with db_session_factory() as session:
        (row,) = await list_channel_skills(session, tenant_id=tenant_id, platform="slack")
    assert (row.channel_id, row.skill_id, row.version) == (CHANNEL, "skill_lib", "v3")
    assert row.added_by_account_id is not None, "the adder is attributed"
    client.views_update.assert_awaited_once_with(view_id="V1", view={"v": 1})

    refused = await _run(db_session_factory, monkeypatch, admin=True, add="missing")
    refused.chat_postEphemeral.assert_awaited_once()
    await _run(db_session_factory, monkeypatch, admin=True, remove=("skill_lib",))
    async with db_session_factory() as session:
        assert await list_channel_skills(session, tenant_id=tenant_id, platform="slack") == []
        events = await list_events(session, tenant_id=tenant_id)
    assert sorted((e.outcome, e.reason) for e in events) == [
        ("allowed", "completed"),
        ("allowed", "completed"),
        ("denied", "not_found"),
    ], "each change and refusal is audited"


async def test_a_non_admin_submission_is_refused_and_stores_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM)
    async with db_session_factory.begin() as session:
        await make_tenant(session, platform="slack", workspace_id=TEAM)
    client = await _run(db_session_factory, monkeypatch, admin=False, add="pdf-tools")
    client.chat_postEphemeral.assert_awaited_once()
    client.views_update.assert_not_awaited()
    async with db_session_factory() as session:
        assert await list_channel_skills(session, tenant_id=tenant_id, platform="slack") == []
        (event,) = await list_events(session, tenant_id=tenant_id)
    assert (event.tool_name, event.outcome, event.reason) == (
        "panel:channel_skills",
        "denied",
        "needs_admin",
    ), "the refusal is audited"

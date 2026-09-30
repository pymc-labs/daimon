"""The Add skill form: preview on first submit, add on an unchanged second, who may."""

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from daimon.adapters.slack import agent_policy
from daimon.adapters.slack.agent_policy import MANAGED_AGENT_MESSAGE, NEEDS_ADMIN_SKILL_MESSAGE
from daimon.adapters.slack.agent_setup import add_skill
from daimon.adapters.slack.agent_setup.add_skill import (
    AddSkillDecision,
    evaluate_add_skill_submission,
    run_add_skill_submission,
)
from daimon.adapters.slack.agent_setup.panel_views import (
    ACTION_ADD_SKILL,
    ADD_SKILL_INPUT_ID,
    LEGACY_ACTION_IDS,
    build_add_skill_form,
)
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    decode_panel_metadata,
    encode_panel_metadata,
)
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.user_skills import load_user_skill
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEAM = "T0SKILLS"
USER = "U0MEMBER"
META = PanelMetadata(
    team_id=TEAM, channel_id="C0ROOM", view="add_skill", agent_name="helper", root_view_id="V1"
)
_MD = "---\nname: notes\ndescription: Take meeting notes.\n---\nWrite them down.\n"


def _payload(text: str, meta: PanelMetadata = META) -> dict[str, Any]:
    typed = {ADD_SKILL_INPUT_ID: {ADD_SKILL_INPUT_ID: {"value": text}}}
    return {"view": {"private_metadata": encode_panel_metadata(meta), "state": {"values": typed}}}


def _previewed(decision: AddSkillDecision) -> PanelMetadata:
    assert decision.response_payload is not None
    assert decision.response_payload["response_action"] == "update"
    view = decision.response_payload["view"]
    meta = decode_panel_metadata(view["private_metadata"])
    assert meta is not None
    return meta


def test_the_first_submit_previews_and_an_unchanged_second_adds() -> None:
    first = evaluate_add_skill_submission(_payload(_MD))
    assert not first.proceed
    view = first.response_payload["view"]  # pyright: ignore[reportOptionalSubscript]
    assert view["submit"]["text"] == "Add"
    assert "Add notes to helper?" in str(view["blocks"])
    meta = _previewed(first)

    again = evaluate_add_skill_submission(_payload(_MD, meta))
    assert again.proceed and again.response_payload is None, "an empty ack closes the form"
    assert again.bundle is not None and again.bundle.preview.name == "notes"

    changed = evaluate_add_skill_submission(_payload(_MD.replace("down", "up"), meta))
    assert not changed.proceed, "changed text is previewed again, never added unseen"
    assert _previewed(changed).skill_hash != meta.skill_hash


def test_an_invalid_skill_is_an_inline_error_and_no_metadata_closes() -> None:
    bad = evaluate_add_skill_submission(_payload("---\nname: Bad\n---\n"))
    assert bad.response_payload is not None
    assert bad.response_payload["response_action"] == "errors"
    assert ADD_SKILL_INPUT_ID in bad.response_payload["errors"]
    assert evaluate_add_skill_submission({"view": {}}) == AddSkillDecision(response_payload=None)


def test_the_form_points_files_to_chat_and_uses_a_fresh_action_id() -> None:
    view = build_add_skill_form(meta=META)
    assert view["submit"]["text"] == "Preview"
    assert "attach it in a message" in str(view["blocks"])
    assert ACTION_ADD_SKILL not in LEGACY_ACTION_IDS


class _World:
    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
        self.factory, self.tenant_id = factory, tenant_id
        self.state = FakeMAState()
        self.skills: list[dict[str, Any]] = []
        self.created: list[str] = []

    def put_agent(self, **metadata: str) -> None:
        agent = ma_agent(
            id="ag_helper",
            name="helper",
            tenant_id=self.tenant_id,
            metadata={"daimon_account": str(uuid.uuid4()), **metadata},
        )
        self.state.agents[agent.id] = agent.model_dump(mode="json")

    def _skills(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/skills":
            raise NotHandled
        if request.method == "GET":
            return list_response(self.skills)
        found = re.search(rb'name="display_title"\r\n\r\n([^\r]+)', request.content)
        assert found is not None
        self.created.append(found.group(1).decode())
        skill = SkillListResponse(
            id=f"skill_{len(self.skills)}",
            type="custom",
            display_title=self.created[-1],
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        self.skills.append(skill)
        return httpx.Response(200, json=skill)

    async def run(self, monkeypatch: pytest.MonkeyPatch, *, admin: bool = False) -> MagicMock:
        monkeypatch.setattr(agent_policy, "resolve_is_admin", AsyncMock(return_value=admin))
        monkeypatch.setattr(add_skill, "resolve_is_admin", AsyncMock(return_value=admin))
        monkeypatch.setattr(add_skill, "load_details_view", AsyncMock(return_value={"v": 1}))
        runtime = MagicMock()
        runtime.sessionmaker = self.factory
        runtime.deployment_default = DeploymentDefault()
        runtime.anthropic = build_fake_anthropic(
            combine_handlers(self._skills, make_fake_ma_handler(self.state))
        )
        client = MagicMock()
        client.chat_postEphemeral = AsyncMock()
        client.views_update = AsyncMock()
        first = evaluate_add_skill_submission(_payload(_MD))
        decision = evaluate_add_skill_submission(_payload(_MD, _previewed(first)))
        await run_add_skill_submission(
            runtime, client, team_id=TEAM, user_id=USER, decision=decision
        )
        return client


async def _world(factory: async_sessionmaker[AsyncSession]) -> _World:
    async with factory.begin() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=TEAM)
    assert tenant.id == derive_tenant_uuid(platform="slack", workspace_id=TEAM)
    return _World(factory, tenant.id)


def _told(client: MagicMock) -> list[str]:
    return [call.kwargs["text"] for call in client.chat_postEphemeral.await_args_list]


async def test_a_member_adds_to_an_agent_that_answers_nowhere_and_details_refresh(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    world.put_agent()

    client = await world.run(monkeypatch)

    assert world.created == [
        tenant_scoped_display_title(tenant_id=world.tenant_id, name="notes", agent_name="helper")
    ], "never the shared library"
    assert _told(client) == ["helper now has the skill *notes*."]
    client.views_update.assert_awaited_once_with(view_id="V1", view={"v": 1})
    async with db_session_factory() as session:
        row = await load_user_skill(
            session,
            tenant_id=world.tenant_id,
            principal_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="ag_helper"),
            agent_name="helper",
            name="notes",
        )
    assert row is not None
    assert (row.source, row.origin) == ("upload", "pasted")
    assert row.added_by_account_id is not None, "the adder is attributed"


async def test_a_member_is_refused_on_the_workspace_default(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    world.put_agent()
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=world.tenant_id),
            tenant_id=world.tenant_id,
            agent_name="helper",
            mode="agent",
        )

    client = await world.run(monkeypatch)

    assert _told(client) == [NEEDS_ADMIN_SKILL_MESSAGE]
    assert world.created == []
    client.views_update.assert_not_awaited()


async def test_a_built_in_agent_is_refused_even_for_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    world.put_agent(daimon_managed="true")

    client = await world.run(monkeypatch, admin=True)

    assert _told(client) == [MANAGED_AGENT_MESSAGE]
    assert world.created == []

"""Add skill from Details, through the real SDK route: preview, then add, and who may."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from daimon.adapters.teams import add_skill, setup_card
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import TenantScopeRef
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.user_skills import load_user_skill
from daimon.testing import build_fake_anthropic, ma_agent
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_card_action,
    make_invoke,
    post_activity,
    running_service,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
SKILL_MD = "---\nname: notes\ndescription: Take meeting notes.\n---\nWrite them down.\n"


class _Skills:
    """The MA skills endpoints: what was uploaded, by display title."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.created: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/skills":
            raise NotHandled
        if request.method == "GET":
            return list_response(self.rows)
        found = re.search(rb'name="display_title"\r\n\r\n([^\r]+)', request.content)
        assert found is not None, "an upload names its display title"
        self.created.append(found.group(1).decode())
        skill = SkillListResponse(
            id=f"skill_{len(self.rows)}",
            type="custom",
            display_title=self.created[-1],
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        self.rows.append(skill)
        return httpx.Response(200, json=skill)


def _state() -> FakeMAState:
    state = FakeMAState()
    owned = {"daimon_account": str(uuid.uuid4())}
    for agent in (
        ma_agent(
            id="agent_daimon",
            tenant_id=TENANT,
            name="daimon",
            metadata={MA_METADATA_KEY_MANAGED: "true"},
        ),
        ma_agent(id="agent_helper", tenant_id=TENANT, name="helper", metadata=owned),
    ):
        state.agents[agent.id] = agent.model_dump(mode="json")
    return state


def _versioned(
    inner: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """The agent fake, with each attached skill's version filled in as MA answers it."""

    def handle(request: httpx.Request) -> httpx.Response:
        response = inner(request)
        if not re.fullmatch(r"/v1/agents/[^/]+", request.url.path):
            return response
        body = response.json()
        for skill in body.get("skills", []):
            skill.setdefault("version", "1")
        return httpx.Response(response.status_code, json=body)

    return handle


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake, skills: _Skills
) -> AsyncIterator[TeamsHttpService]:
    ma = build_fake_anthropic(
        combine_handlers(skills.handle, _versioned(make_fake_ma_handler(_state())))
    )
    runtime = build_teams_runtime(db_factory, anthropic=ma, teams=teams_settings())
    async with running_service(runtime, fake) as service:
        yield service


async def _dialog(service: TeamsHttpService, kind: str, **data: object) -> Any:
    if kind == "fetch":
        data = {"dialog_id": setup_card.SKILL_DIALOG} | data
    else:
        data = {"action": setup_card.SKILL_DIALOG} | data
    return await post_activity(service, make_invoke(f"task/{kind}", {"data": data}))


def _form(response: Any) -> dict[str, Any]:
    assert response["task"]["type"] == "continue", response
    return response["task"]["value"]["card"]["content"]


def _submit_data(form: dict[str, Any]) -> dict[str, Any]:
    [submit] = form["actions"]
    return submit["data"]


async def test_details_offers_add_skill_only_on_an_agent_that_can_take_one(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake, _Skills()) as service:
        helper = await post_activity(
            service, make_card_action("agent_setup", "details", agent="helper")
        )
        built_in = await post_activity(
            service, make_card_action("agent_setup", "details", agent="daimon")
        )
    assert "Add skill" in json.dumps(helper), "a member's own agent can take a skill"
    assert "Add skill" not in json.dumps(built_in), "a starting agent is never changed"


async def test_a_paste_is_previewed_then_added_as_the_agents_own_and_details_refresh(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    skills = _Skills()
    async with _running(db_session_factory, teams_api_fake, skills) as service:
        opened = _form(await _dialog(service, "fetch", agent="helper"))
        preview = _form(await _dialog(service, "submit", agent="helper", skill=SKILL_MD))
        changed = _form(
            await _dialog(
                service, "submit", **_submit_data(preview), skill=SKILL_MD.replace("down", "up")
            )
        )
        added = await _dialog(service, "submit", **_submit_data(preview), skill=SKILL_MD)

    assert "Preview" in json.dumps(opened) and "Add helper" not in json.dumps(opened)
    assert "Add notes to helper?" in json.dumps(preview) and "SKILL.md" in json.dumps(preview)
    assert _submit_data(changed)["hash"] != _submit_data(preview)["hash"], (
        "changed: previewed again"
    )
    assert added["task"]["value"] == "helper now has the skill notes."
    assert skills.created == [
        tenant_scoped_display_title(tenant_id=TENANT, name="notes", agent_name="helper")
    ], "uploaded once, as the agent's own copy, never to the shared library"
    edits = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert edits and edits[-1].url.endswith("/activities/m-7"), "the panel shows Details again"
    async with db_session_factory() as session:
        row = await load_user_skill(
            session,
            tenant_id=TENANT,
            principal_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id="agent_helper"),
            agent_name="helper",
            name="notes",
        )
    assert row is not None and row.origin == "pasted", "recorded as a paste"
    assert row.added_by_account_id is not None, "the adder is attributed"


async def test_an_invalid_paste_comes_back_with_the_reason_and_adds_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    skills = _Skills()
    async with _running(db_session_factory, teams_api_fake, skills) as service:
        again = _form(await _dialog(service, "submit", agent="helper", skill="no frontmatter"))
    assert "hash" not in _submit_data(again), "nothing to confirm yet"
    assert "no frontmatter" in json.dumps(again), "the paste is kept to fix"
    assert skills.created == []


async def test_a_built_in_agent_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake, _Skills()) as service:
        opened = await _dialog(service, "fetch", agent="daimon")
    assert opened["task"]["value"] == add_skill.BUILT_IN


async def test_a_member_is_refused_on_the_organisation_default(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=TENANT),
            tenant_id=TENANT,
            agent_name="helper",
            mode="agent",
        )
    skills = _Skills()
    async with _running(db_session_factory, teams_api_fake, skills) as service:
        opened = await _dialog(service, "fetch", agent="helper")
        preview = _form(await _dialog(service, "submit", agent="helper", skill=SKILL_MD))
        forced = await _dialog(service, "submit", **_submit_data(preview), skill=SKILL_MD)

    assert opened["task"]["value"] == add_skill.needs_admin("helper"), "others depend on it"
    assert forced["task"]["value"] == add_skill.needs_admin("helper"), "decided again on add"
    assert skills.created == [], "nothing uploaded"

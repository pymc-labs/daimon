"""`daimon skills add`: one skill onto one agent, through core's add path.

Real Postgres for the tenant and the skill ledger; a transport-level fake MA
(`MARouter`) for the agent and skill calls.
"""

from __future__ import annotations

import json
import re
import uuid
from io import StringIO
from pathlib import Path
from typing import cast

import httpx
import pytest
import typer
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta import BetaSkill as SkillListResponse
from daimon.adapters.cli.commands.skills import add_skill
from daimon.adapters.cli.runtime import CliRuntime
from daimon.adapters.cli.tenant import TenantSelector
from daimon.core.config import Settings
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.user_skills import load_user_skill
from daimon.testing import MARouter, build_fake_anthropic, list_response, ma_agent
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TS = "2026-04-21T00:00:00Z"
_SKILL_MD = b"---\nname: notes\ndescription: Take meeting notes.\n---\nWrite them down.\n"


class _FakeCli:
    local_user = "testuser"


class _FakeGithub:
    max_tarball_bytes = 10_000_000
    max_tarball_decompressed_bytes = 50_000_000


class _FakeSettings:
    cli = _FakeCli()
    github = _FakeGithub()


class _FakeMA:
    """The agent and skill endpoints `add_agent_skill` calls, recording the writes."""

    def __init__(self, agent: BetaManagedAgentsAgent) -> None:
        self.agent = agent
        self.created_titles: list[str] = []
        self.updates: list[dict[str, object]] = []

    def router(self) -> MARouter:
        router = MARouter()
        router.add("GET", r"/v1/skills", lambda _r, _m: list_response([]))
        router.add("POST", r"/v1/skills", self._create)
        router.add("GET", rf"/v1/agents/{self.agent.id}", self._agent)
        router.add(
            "GET",
            r"/v1/agents",
            lambda _r, _m: list_response([self.agent.model_dump(mode="json")]),
        )
        router.add("POST", rf"/v1/agents/{self.agent.id}", self._update)
        return router

    def _create(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        found = re.search(rb'name="display_title"\r\n\r\n([^\r]+)', request.content)
        assert found is not None, "skills.create must send a display_title"
        self.created_titles.append(found.group(1).decode())
        created = SkillListResponse(
            id="sk_new",
            type="custom",
            display_title=found.group(1).decode(),
            latest_version="1",
            created_at=_TS,
            updated_at=_TS,
            source="custom",
        )
        return httpx.Response(200, json=created.model_dump(mode="json"))

    def _agent(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=self.agent.model_dump(mode="json"))

    def _update(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        self.updates.append(json.loads(request.content))
        return httpx.Response(200, json=self.agent.model_dump(mode="json"))


def _rt(factory: async_sessionmaker[AsyncSession], fake: _FakeMA) -> CliRuntime:
    rt = cast(CliRuntime, object.__new__(CliRuntime))
    object.__setattr__(rt, "settings", cast(Settings, _FakeSettings()))
    object.__setattr__(rt, "anthropic", build_fake_anthropic(fake.router().dispatch))
    object.__setattr__(rt, "sessionmaker", factory)
    return rt


def _skill_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(_SKILL_MD)
    return folder


async def _tenant_agent(
    db_session: AsyncSession, *, metadata: dict[str, str] | None = None
) -> tuple[uuid.UUID, BetaManagedAgentsAgent]:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    stamp = {"daimon_account": str(uuid.uuid4())}
    agent = ma_agent(
        id="agent_helper", name="helper", tenant_id=tenant.id, metadata=stamp | (metadata or {})
    )
    return tenant.id, agent


async def test_skills_add_uploads_a_local_folder_and_attaches_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant_id, agent = await _tenant_agent(db_session)
    fake = _FakeMA(agent)
    buf = StringIO()

    await add_skill(
        _rt(db_session_factory, fake),
        Console(file=buf, width=200),
        agent_name="helper",
        source=str(_skill_folder(tmp_path)),
        yes=True,
        selector=TenantSelector(tenant_id=str(tenant_id)),
    )

    assert len(fake.created_titles) == 1 and fake.created_titles[0].endswith("helper/notes"), (
        f"the skill is uploaded under the agent's own title: {fake.created_titles}"
    )
    assert fake.updates and fake.updates[-1]["skills"] == [
        {"type": "custom", "skill_id": "sk_new"}
    ], f"and attached to the agent: {fake.updates}"
    async with db_session_factory() as session:
        row = await load_user_skill(
            session,
            tenant_id=tenant_id,
            principal_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id),
            agent_name="helper",
            name="notes",
        )
    assert row is not None and row.source == "upload", "the ledger records it as an upload"
    assert "added skill 'notes'" in buf.getvalue(), buf.getvalue()


@pytest.mark.parametrize(
    "metadata",
    [{"daimon_managed": "true"}, None],
    ids=["defaults-managed", "unstamped-system"],
)
async def test_skills_add_refuses_a_built_in_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    metadata: dict[str, str] | None,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    tenant_id = tenant.id
    agent = ma_agent(id="agent_helper", name="helper", tenant_id=tenant_id, metadata=metadata)
    fake = _FakeMA(agent)

    with pytest.raises(StoreError, match="built-in agent"):
        await add_skill(
            _rt(db_session_factory, fake),
            Console(file=StringIO()),
            agent_name="helper",
            source=str(_skill_folder(tmp_path)),
            yes=True,
            selector=TenantSelector(tenant_id=str(tenant_id)),
        )
    assert not fake.created_titles and not fake.updates, "nothing is uploaded or attached"


async def test_skills_add_uploads_nothing_when_the_prompt_is_declined(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, agent = await _tenant_agent(db_session)
    fake = _FakeMA(agent)
    monkeypatch.setattr(typer, "confirm", lambda *_a, **_k: False)

    with pytest.raises(typer.Exit):
        await add_skill(
            _rt(db_session_factory, fake),
            Console(file=StringIO()),
            agent_name="helper",
            source=str(_skill_folder(tmp_path)),
            yes=False,
            selector=TenantSelector(tenant_id=str(tenant_id)),
        )
    assert not fake.created_titles and not fake.updates, "a declined prompt changes nothing"


async def test_skills_add_refuses_an_unknown_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant_id, agent = await _tenant_agent(db_session)
    fake = _FakeMA(agent)
    with pytest.raises(StoreError, match="no agent named 'other'"):
        await add_skill(
            _rt(db_session_factory, fake),
            Console(file=StringIO()),
            agent_name="other",
            source=str(_skill_folder(tmp_path)),
            yes=True,
            selector=TenantSelector(tenant_id=str(tenant_id)),
        )

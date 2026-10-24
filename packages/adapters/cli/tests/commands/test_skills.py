from __future__ import annotations

import asyncio
import re
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, SkillCreateResponse, SkillListResponse
from anthropic.types.beta.skills import VersionCreateResponse
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands import skills as skills_cmd
from daimon.adapters.cli.commands.skills import (
    delete_skill,
    get_skill,
    list_skills,
    sync_skills,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.attachments import SkillStillAttached
from daimon.core.stores.channel_skills import add_channel_skill, list_skill_channel_rules
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.seeded_skills import record_seeded_skill
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, json_body, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# Shared helpers (not imported — each test file is self-contained)
# ---------------------------------------------------------------------------


def _make_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> CliRuntime:
    rt = cast(CliRuntime, object.__new__(CliRuntime))

    class _Cli:
        local_user = "testuser"

    class _Settings:
        cli = _Cli()

    object.__setattr__(rt, "settings", _Settings())
    object.__setattr__(rt, "anthropic", stub_anthropic)
    object.__setattr__(rt, "sessionmaker", db_session_factory)
    return rt


def _build_rt_with_router(
    db_session_factory: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> CliRuntime:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client)
    return _make_rt(db_session_factory, client)


# ---------------------------------------------------------------------------
# Response helpers (validated SDK constructors per guideline:testing)
# ---------------------------------------------------------------------------


def _skill_row(id_: str, name: str, *, source: str = "custom") -> dict[str, object]:
    return SkillListResponse(
        id=id_,
        type="custom",
        display_title=name,
        latest_version="1",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source=source,
    ).model_dump(mode="json")


def _version_row(id_: str, skill_id: str) -> dict[str, object]:
    return VersionCreateResponse(
        id=id_,
        skill_id=skill_id,
        type="skill_version",
        version="1",
        name="v1",
        description="",
        directory="/",
        created_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")


def _tracking_fetch(captured: list[Path]):
    """Return a fake fetch_repo that records the temp dir it creates into `captured`."""
    from daimon.core.skills.fetch import FetchResult

    async def _fetch(
        http_client: Any,
        url: str,
        *,
        branch: str = "main",
        token: str | None = None,
        max_tarball_bytes: int = 50 * 1024 * 1024,
        max_tarball_decompressed_bytes: int = 200 * 1024 * 1024,
    ) -> FetchResult:
        tmp = Path(tempfile.mkdtemp())
        captured.append(tmp)
        skill_dir = tmp / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: A test skill\n---\nBody content"
        )
        return FetchResult(path=tmp, cleanup_dir=tmp)

    return _fetch


# ---------------------------------------------------------------------------
# list tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skills_list_shows_own_tenant_bare_names_and_builtin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """list shows own tenant's skills (by bare name) plus anthropic built-ins;
    a foreign tenant's canonical title must not appear in stdout."""
    async with db_session_factory() as s, s.begin():
        tenant_a = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant_a is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant_a.id, os_user="testuser")

    tenant_b_id = uuid.uuid4()
    own_title = tenant_scoped_display_title(tenant_id=tenant_a.id, name="alpha")
    foreign_title = tenant_scoped_display_title(tenant_id=tenant_b_id, name="beta")
    builtin_title = "anthropic-builtin-tool"

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [
                _skill_row("sk_1", own_title),
                _skill_row("sk_2", foreign_title),
                _skill_row("sk_3", builtin_title, source="anthropic"),
            ]
        ),
    )

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    await list_skills(rt, console, as_json=False)

    out = cast(StringIO, console.file).getvalue()
    assert own_title in out, "list output must include own tenant's canonical skill title"
    assert builtin_title in out, "list output must include anthropic built-in skill"
    assert foreign_title not in out, "list output must NOT include foreign tenant's skill title"


@pytest.mark.asyncio
async def test_skills_list_returns_table(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """list fetches skills from MA and emits a table with display_title column."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    own_alpha = tenant_scoped_display_title(tenant_id=tenant.id, name="alpha")
    own_beta = tenant_scoped_display_title(tenant_id=tenant.id, name="beta")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [_skill_row("sk_1", own_alpha), _skill_row("sk_2", own_beta)]
        ),
    )

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    await list_skills(rt, console, as_json=False)

    out = cast(StringIO, console.file).getvalue()
    assert own_alpha in out, "list output must include first skill canonical title"
    assert own_beta in out, "list output must include second skill canonical title"


@pytest.mark.asyncio
async def test_skills_list_json(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """list with as_json=True emits valid JSON containing skill names."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    own_alpha = tenant_scoped_display_title(tenant_id=tenant.id, name="alpha")
    own_beta = tenant_scoped_display_title(tenant_id=tenant.id, name="beta")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [_skill_row("sk_1", own_alpha), _skill_row("sk_2", own_beta)]
        ),
    )

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    await list_skills(rt, console, as_json=True)

    import json

    out = cast(StringIO, console.file).getvalue()
    rows = json.loads(out)
    names = [r["display_title"] for r in rows]
    assert own_alpha in names, "JSON output must include first skill canonical title"
    assert own_beta in names, "JSON output must include second skill canonical title"


# ---------------------------------------------------------------------------
# get tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skills_get_shows_detail(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get resolves bare name to canonical title and prints detail with version count."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    canonical = tenant_scoped_display_title(tenant_id=tenant.id, name="brainstorming")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response([_skill_row("sk_1", canonical)]),
    )
    router.add(
        "GET",
        r"/v1/skills/sk_1/versions",
        lambda req, _m: list_response(
            [
                _version_row("sv_1", "sk_1"),
                _version_row("sv_2", "sk_1"),
                _version_row("sv_3", "sk_1"),
            ]
        ),
    )

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    await get_skill(rt, console, name="brainstorming", as_json=False)

    out = buf.getvalue()
    assert "Versions: 3" in out, "get detail must show version count 'Versions: 3'"


@pytest.mark.asyncio
async def test_skills_get_foreign_bare_name_raises_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get with a foreign tenant's bare name resolves to a canonical that doesn't exist;
    must raise StoreError (not-found path)."""
    async with db_session_factory() as s, s.begin():
        tenant_a = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant_a is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant_a.id, os_user="testuser")

    tenant_b_id = uuid.uuid4()
    # MA only has the skill under tenant_b's canonical title
    foreign_canonical = tenant_scoped_display_title(tenant_id=tenant_b_id, name="brainstorming")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        # Responding with foreign tenant's canonical title; tenant_a lookup won't match
        lambda req, _m: list_response([_skill_row("sk_2", foreign_canonical)]),
    )

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    with pytest.raises(StoreError, match="no skill named"):
        await get_skill(rt, console, name="brainstorming", as_json=False)


@pytest.mark.asyncio
async def test_skills_get_not_found_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get raises StoreError when no skill matches the name."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    with pytest.raises(StoreError, match="no skill named"):
        await get_skill(rt, console, name="ghost", as_json=False)


# ---------------------------------------------------------------------------
# delete tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skills_delete_with_yes_hits_delete_request(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delete with yes=True resolves own bare name to canonical and sends DELETE request."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    canonical = tenant_scoped_display_title(tenant_id=tenant.id, name="my-skill")
    delete_requests: list[httpx.Request] = []

    def _capture_delete(req: httpx.Request, _m: object) -> httpx.Response:
        delete_requests.append(req)
        return httpx.Response(200, json={"id": "sk_1", "type": "deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response([_skill_row("sk_1", canonical)]),
    )
    router.add(
        "GET",
        r"/v1/skills/[^/]+/versions",
        lambda req, _m: list_response([]),
    )
    router.add("DELETE", r"/v1/skills/[^/]+", _capture_delete)
    router.add_agent_list()

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    await delete_skill(rt, console, name="my-skill", yes=True)

    assert len(delete_requests) == 1, "delete must send exactly one DELETE request to the stub"


@pytest.mark.asyncio
async def test_skills_delete_with_yes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delete with yes=True resolves skill and calls skills.delete(id)."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    canonical = tenant_scoped_display_title(tenant_id=tenant.id, name="my-skill")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response([_skill_row("sk_1", canonical)]),
    )
    router.add(
        "GET",
        r"/v1/skills/[^/]+/versions",
        lambda req, _m: list_response([]),
    )
    router.add(
        "DELETE",
        r"/v1/skills/[^/]+",
        lambda req, _m: httpx.Response(200, json={"id": "sk_1", "type": "deleted"}),
    )
    router.add_agent_list()

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    # Should not raise — deletion succeeds
    await delete_skill(rt, console, name="my-skill", yes=True)


@pytest.mark.asyncio
async def test_skills_delete_foreign_bare_name_raises_not_found_no_delete_requests(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delete with a foreign tenant's bare name resolves to a canonical that doesn't match;
    must raise StoreError and send zero DELETE requests."""
    async with db_session_factory() as s, s.begin():
        tenant_a = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant_a is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant_a.id, os_user="testuser")

    tenant_b_id = uuid.uuid4()
    foreign_canonical = tenant_scoped_display_title(tenant_id=tenant_b_id, name="my-skill")
    delete_requests: list[httpx.Request] = []

    def _capture_delete(req: httpx.Request, _m: object) -> httpx.Response:
        delete_requests.append(req)
        return httpx.Response(200, json={"id": "sk_2", "type": "deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        # Only has the foreign tenant's skill
        lambda req, _m: list_response([_skill_row("sk_2", foreign_canonical)]),
    )
    router.add("DELETE", r"/v1/skills/[^/]+", _capture_delete)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    with pytest.raises(StoreError, match="no skill named"):
        await delete_skill(rt, console, name="my-skill", yes=True)

    assert len(delete_requests) == 0, "delete must send zero DELETE requests when skill not found"


@pytest.mark.asyncio
async def test_skills_delete_not_found_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delete raises StoreError when no skill matches the name."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    with pytest.raises(StoreError, match="no skill named"):
        await delete_skill(rt, console, name="ghost", yes=True)


def _attached_delete_router(
    canonical: str,
    *,
    agents: list[BetaManagedAgentsAgent],
    deletes: list[httpx.Request],
    updates: list[dict[str, Any]],
) -> MARouter:
    """A skill `sk_1` titled `canonical`, the given agents, and capture of every write."""
    by_id = {agent.id: agent for agent in agents}

    def _delete(req: httpx.Request, _m: object) -> httpx.Response:
        deletes.append(req)
        return httpx.Response(200, json={"id": "sk_1", "type": "deleted"})

    def _retrieve(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=by_id[m.group(1)].model_dump(mode="json"))

    def _update(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        updates.append(body)
        fresh = by_id[m.group(1)]
        updated = ma_agent(
            id=fresh.id,
            name=fresh.name,
            metadata=fresh.metadata,
            skills=body["skills"],
            version=fresh.version + 1,
        )
        return httpx.Response(200, json=updated.model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda _r, _m: list_response([_skill_row("sk_1", canonical)]))
    router.add("GET", r"/v1/skills/[^/]+/versions", lambda _r, _m: list_response([]))
    router.add("DELETE", r"/v1/skills/[^/]+", _delete)
    router.add_agent_list(*agents)
    router.add("GET", r"/v1/agents/([^/]+)", _retrieve)
    router.add("POST", r"/v1/agents/([^/]+)", _update)
    return router


async def _cli_tenant(db_session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
    return tenant.id


@pytest.mark.asyncio
async def test_skills_delete_refuses_while_an_agent_uses_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """QA-NEW19: deleting a skill an agent pins is refused, naming the agent; nothing is deleted."""
    tenant_id = await _cli_tenant(db_session_factory)
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name="my-skill")
    agent = ma_agent(
        id="agent_uses",
        name="research-bot",
        tenant_id=tenant_id,
        skills=[{"type": "custom", "skill_id": "sk_1", "version": "latest"}],
    )
    deletes: list[httpx.Request] = []
    updates: list[dict[str, Any]] = []
    router = _attached_delete_router(canonical, agents=[agent], deletes=deletes, updates=updates)
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    with pytest.raises(SkillStillAttached) as refused:
        await delete_skill(rt, console, name="my-skill", yes=True)

    message = str(refused.value)
    assert re.search(r"(?i)skill is still attached|still attached to", message), message
    assert "research-bot" in message, "the refusal names the agent the skill is attached to"
    assert "remove_skill" in message and "--force" in message, "it says how to detach"
    assert deletes == [] and updates == [], "a refused delete writes nothing"


@pytest.mark.asyncio
async def test_skills_delete_refuses_while_a_channel_rule_adds_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A skill only a channel rule adds is still in use: refused, naming the channel."""
    tenant_id = await _cli_tenant(db_session_factory)
    async with db_session_factory() as s, s.begin():
        await add_channel_skill(
            s,
            tenant_id=tenant_id,
            platform="discord",
            channel_id="555",
            skill_id="sk_1",
            version="1791633132940669",
            name="my-skill",
            owner_agent_name=None,
            actor_account_id=None,
        )
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name="my-skill")
    deletes: list[httpx.Request] = []
    router = _attached_delete_router(canonical, agents=[], deletes=deletes, updates=[])
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    with pytest.raises(SkillStillAttached, match="still attached to") as refused:
        await delete_skill(rt, console, name="my-skill", yes=True)

    assert "discord channel 555" in str(refused.value)
    assert "daimon channels skills remove" in str(refused.value)
    assert deletes == []


@pytest.mark.asyncio
async def test_skills_delete_force_detaches_everywhere_then_deletes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """--force removes the skill from each agent (keeping its other skills) and each channel."""
    tenant_id = await _cli_tenant(db_session_factory)
    async with db_session_factory() as s, s.begin():
        await add_channel_skill(
            s,
            tenant_id=tenant_id,
            platform="discord",
            channel_id="555",
            skill_id="sk_1",
            version="1",
            name="my-skill",
            owner_agent_name=None,
            actor_account_id=None,
        )
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name="my-skill")
    agent = ma_agent(
        id="agent_uses",
        name="research-bot",
        tenant_id=tenant_id,
        skills=[
            {"type": "custom", "skill_id": "sk_1", "version": "latest"},
            {"type": "custom", "skill_id": "sk_keep", "version": "7"},
        ],
    )
    deletes: list[httpx.Request] = []
    updates: list[dict[str, Any]] = []
    router = _attached_delete_router(canonical, agents=[agent], deletes=deletes, updates=updates)
    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    await delete_skill(rt, console, name="my-skill", yes=True, force=True)

    assert len(updates) == 1
    assert updates[0]["skills"] == [{"type": "custom", "skill_id": "sk_keep", "version": "7"}], (
        "the agent keeps its other skills at the versions it pinned"
    )
    async with db_session_factory() as s:
        assert await list_skill_channel_rules(s, skill_id="sk_1") == []
    assert len(deletes) == 1, "the skill is deleted after it is detached"
    assert "detached 'my-skill' from 1 agent(s) and 1 channel rule(s)" in out.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_skills_delete_honours_a_channel_rule_committed_after_the_first_look(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
) -> None:
    """PR #857 review repro: a rule committed between the preview and the delete.

    The preview finds nothing; another request then commits a channel rule.
    The decision is made again under the skill's lock, so a plain delete is
    refused and --force removes the new rule too: no rule is left pinning a
    deleted skill.
    """
    tenant_id = await _cli_tenant(db_session_factory)
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name="my-skill")
    deletes: list[httpx.Request] = []
    router = _attached_delete_router(canonical, agents=[], deletes=deletes, updates=[])
    pending: list[asyncio.Task[None]] = []

    async def _commit_rule() -> None:
        async with db_session_factory() as s, s.begin():
            await add_channel_skill(
                s,
                tenant_id=tenant_id,
                platform="discord",
                channel_id="new-channel",
                skill_id="sk_1",
                version="1",
                name="my-skill",
                owner_agent_name=None,
                actor_account_id=None,
            )

    def _confirm_while_another_request_attaches(
        _console: Console, _question: str, *, yes: bool
    ) -> None:
        # Runs after the preview found nothing and before the delete.
        pending.append(asyncio.ensure_future(_commit_rule()))

    monkeypatch.setattr(skills_cmd, "confirm_or_abort", _confirm_while_another_request_attaches)
    real_delete = skills_cmd.delete_skill_unless_attached

    async def _delete_after_the_rule_commits(*args: Any, **kwargs: Any) -> Any:
        await asyncio.gather(*pending)
        return await real_delete(*args, **kwargs)

    monkeypatch.setattr(skills_cmd, "delete_skill_unless_attached", _delete_after_the_rule_commits)
    rt = _build_rt_with_router(db_session_factory, router)
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    if force:
        await delete_skill(rt, console, name="my-skill", yes=True, force=True)
    else:
        with pytest.raises(SkillStillAttached, match="new-channel"):
            await delete_skill(rt, console, name="my-skill", yes=True)

    async with db_session_factory() as s:
        dangling = await list_skill_channel_rules(s, skill_id="sk_1")
    if force:
        assert dangling == [] and len(deletes) == 1
    else:
        assert len(dangling) == 1 and deletes == []


@pytest.mark.asyncio
async def test_skills_delete_force_confirmation_names_the_cross_tenant_scope(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force detaches references in every tenant; the prompt says so, with counts."""
    tenant_id = await _cli_tenant(db_session_factory)
    async with db_session_factory() as s, s.begin():
        foreign = await make_tenant(s, platform="discord", workspace_id="foreign-guild")
        await add_channel_skill(
            s,
            tenant_id=foreign.id,
            platform="discord",
            channel_id="foreign-channel",
            skill_id="sk_1",
            version="1",
            name="my-skill",
            owner_agent_name=None,
            actor_account_id=None,
        )
    agents = [
        ma_agent(
            id="agent_mine",
            name="mine",
            tenant_id=tenant_id,
            skills=[{"type": "custom", "skill_id": "sk_1", "version": "1"}],
        ),
        ma_agent(
            id="agent_foreign",
            name="foreign",
            tenant_id=foreign.id,
            skills=[{"type": "custom", "skill_id": "sk_1", "version": "1"}],
        ),
    ]
    deletes: list[httpx.Request] = []
    updates: list[dict[str, Any]] = []
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name="my-skill")
    router = _attached_delete_router(canonical, agents=agents, deletes=deletes, updates=updates)
    questions: list[str] = []

    def _confirm(_console: Console, question: str, *, yes: bool) -> None:
        questions.append(question)

    monkeypatch.setattr(skills_cmd, "confirm_or_abort", _confirm)
    rt = _build_rt_with_router(db_session_factory, router)
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    await delete_skill(rt, console, name="my-skill", yes=False, force=True)

    assert len(questions) == 1
    assert "2 agent(s) and 1 channel rule(s) across ALL tenants" in questions[0]
    assert "1 agent(s) and 1 channel rule(s) outside" in questions[0]
    assert len(updates) == 2, "both tenants' agents are detached"
    async with db_session_factory() as s:
        assert await list_skill_channel_rules(s, skill_id="sk_1") == []
    assert len(deletes) == 1


def test_skills_delete_cli_exits_1_with_the_refusal(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command itself exits 1 with the refusal QA-NEW19 matches on."""

    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            tenant = await make_tenant(s, platform="cli", workspace_id="local")
        return tenant.id

    tenant_id = asyncio.run(seed())
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name="qa-echo-skill")
    agent = ma_agent(
        id="agent_uses",
        name="qa-fork",
        tenant_id=tenant_id,
        skills=[{"type": "custom", "skill_id": "sk_1", "version": "latest"}],
    )
    deletes: list[httpx.Request] = []
    router = _attached_delete_router(canonical, agents=[agent], deletes=deletes, updates=[])
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(router.dispatch), base_url="https://api.anthropic.com"
    )
    anthropic = AsyncAnthropic(api_key="test", http_client=http_client)
    _install_skills_runtime(monkeypatch, anthropic=anthropic, sessionmaker=schema_sessionmaker)

    result = CliRunner().invoke(main_mod.app, ["skills", "delete", "qa-echo-skill", "--yes"])

    assert result.exit_code == 1, result.stdout
    assert re.search(r"(?i)skill is still attached|still attached to", result.stdout)
    assert deletes == []


# ---------------------------------------------------------------------------
# sync tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skills_sync_happy_path(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sync fetches a repo, discovers skills, and creates them on MA."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    captured: list[Path] = []
    monkeypatch.setattr(
        "daimon.core.skills.pipeline.fetch_repo",
        _tracking_fetch(captured),
    )

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add(
        "POST",
        r"/v1/skills",
        lambda req, _m: httpx.Response(
            200,
            json=SkillCreateResponse(
                id="sk_new",
                type="custom",
                display_title="my-skill",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        ),
    )

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b""))
    )
    await sync_skills(
        rt,
        console,
        url="https://github.com/org/repo",
        branch="main",
        path="",
        http_client=http_client,
    )

    assert len(captured) == 1, "fetch_repo must have been called exactly once"
    out = buf.getvalue()
    assert "my-skill" in out, "sync outcome table must contain skill name 'my-skill'"
    assert "created" in out, "sync outcome table must contain action 'created'"


@pytest.mark.asyncio
async def test_skills_sync_refuses_a_seeded_skill_name(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo skill named like a seeded one is reported failed and never uploaded."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        await record_seeded_skill(
            s, tenant_id=tenant.id, name="my-skill", content_hash="h", anthropic_id="sk_seeded"
        )

    monkeypatch.setattr("daimon.core.skills.pipeline.fetch_repo", _tracking_fetch([]))
    posts: list[str] = []

    def on_post(req: httpx.Request, _m: object) -> httpx.Response:
        posts.append(req.url.path)
        return httpx.Response(500)

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add("POST", r"/v1/skills", on_post)
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=200)

    await sync_skills(
        _build_rt_with_router(db_session_factory, router),
        console,
        url="https://github.com/org/repo",
        branch="main",
        path="",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b""))
        ),
    )

    assert posts == [], "the seeded skill must not be created or versioned"
    assert "failed" in buf.getvalue(), "the outcome table reports the refusal"


@pytest.mark.asyncio
async def test_skills_sync_cleans_temp_dir(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sync removes the temp directory created by fetch_repo after completion."""
    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    captured: list[Path] = []
    monkeypatch.setattr(
        "daimon.core.skills.pipeline.fetch_repo",
        _tracking_fetch(captured),
    )

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add(
        "POST",
        r"/v1/skills",
        lambda req, _m: httpx.Response(
            200,
            json=SkillCreateResponse(
                id="sk_new",
                type="custom",
                display_title="my-skill",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        ),
    )

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b""))
    )
    await sync_skills(
        rt,
        console,
        url="https://github.com/org/repo",
        branch="main",
        path="",
        http_client=http_client,
    )

    assert len(captured) == 1, "fetch_repo must have been called exactly once"
    assert not captured[0].exists(), "sync must clean up the temp dir created by fetch_repo"


@pytest.mark.asyncio
async def test_skills_sync_with_branch_and_path(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sync with non-default --branch and --path resolves target via result.path / path."""
    from daimon.core.skills.fetch import FetchResult

    async with db_session_factory() as s, s.begin():
        tenant = await get_tenant(s, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None, "conftest autoseeds the cli:local tenant"
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    captured: list[Path] = []

    def _tracking_fetch_with_subdir(captured_list: list[Path]):
        async def _fetch(
            http_client: Any,
            url: str,
            *,
            branch: str = "main",
            token: str | None = None,
            max_tarball_bytes: int = 50 * 1024 * 1024,
            max_tarball_decompressed_bytes: int = 200 * 1024 * 1024,
        ) -> FetchResult:
            tmp = Path(tempfile.mkdtemp())
            captured_list.append(tmp)
            skill_dir = tmp / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: A test skill\n---\nBody content"
            )
            return FetchResult(path=tmp, cleanup_dir=tmp)

        return _fetch

    monkeypatch.setattr(
        "daimon.core.skills.pipeline.fetch_repo",
        _tracking_fetch_with_subdir(captured),
    )

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add(
        "POST",
        r"/v1/skills",
        lambda req, _m: httpx.Response(
            200,
            json=SkillCreateResponse(
                id="sk_new",
                type="custom",
                display_title="my-skill",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        ),
    )

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_router(db_session_factory, router)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b""))
    )
    await sync_skills(
        rt,
        console,
        url="https://github.com/org/repo",
        branch="develop",
        path="skills",
        http_client=http_client,
    )

    assert len(captured) == 1, "fetch_repo must have been called exactly once"


# Keep Settings import used in type annotations
_ = Settings


# ---------------------------------------------------------------------------
# --tenant/--guild override (behavioral, via CliRunner + the real Typer app)
# ---------------------------------------------------------------------------


def _install_skills_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    class _Cli:
        local_user = "testuser"

    class _FakeSettings:
        cli = _Cli()

    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", cast(Settings, _FakeSettings()))
    object.__setattr__(rt, "anthropic", anthropic)
    object.__setattr__(rt, "sessionmaker", sessionmaker)
    object.__setattr__(rt, "deployment_default", DeploymentDefault())
    object.__setattr__(rt, "resolver_cache", new_resolver_cache())

    @asynccontextmanager
    async def fake_build_runtime(_settings: Settings) -> AsyncIterator[CliRuntime]:
        yield rt

    monkeypatch.setattr(skills_cmd, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(skills_cmd, "load_settings", lambda: cast(Settings, _FakeSettings()))


def test_skills_list_with_guild_override_lists_that_guilds_skills(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--guild reaches this group's discover_tenant call too, not just agents'."""
    guild_id = "skills-override-guild"
    guild_tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

    async def seed() -> None:
        async with schema_sessionmaker() as s, s.begin():
            await make_tenant(s, platform="cli", workspace_id="local")
            await make_tenant(s, platform="discord", workspace_id=guild_id, id=guild_tenant_id)

    asyncio.run(seed())

    canonical = tenant_scoped_display_title(tenant_id=guild_tenant_id, name="guild-only-skill")
    router = MARouter()
    router.add(
        "GET", r"/v1/skills", lambda req, m: list_response([_skill_row("sk_guild", canonical)])
    )
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    anthropic = AsyncAnthropic(api_key="test", http_client=http_client)

    _install_skills_runtime(monkeypatch, anthropic=anthropic, sessionmaker=schema_sessionmaker)

    without_override = CliRunner().invoke(main_mod.app, ["skills", "list"])
    assert without_override.exit_code == 0, without_override.stdout
    assert "guild-only-skill" not in without_override.stdout, (
        "the local cli:local tenant must not see another guild's skills"
    )

    with_override = CliRunner().invoke(main_mod.app, ["skills", "--guild", guild_id, "list"])
    assert with_override.exit_code == 0, with_override.stdout
    assert "guild-only-skill" in with_override.stdout, (
        "--guild must resolve to that guild's tenant and list its skills"
    )


@pytest.mark.asyncio
async def test_skills_delete_force_with_nothing_attached_still_names_all_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #857 re-review: --force reaches references added after the preview, in any tenant."""
    tenant_id = await _cli_tenant(db_session_factory)
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name="my-skill")
    deletes: list[httpx.Request] = []
    router = _attached_delete_router(canonical, agents=[], deletes=deletes, updates=[])
    questions: list[str] = []

    def _confirm(_console: Console, question: str, *, yes: bool) -> None:
        questions.append(question)

    monkeypatch.setattr(skills_cmd, "confirm_or_abort", _confirm)
    rt = _build_rt_with_router(db_session_factory, router)
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    await delete_skill(rt, console, name="my-skill", yes=False, force=True)

    assert len(questions) == 1
    assert "0 agent(s) and 0 channel rule(s) across ALL tenants" in questions[0]
    assert "plus any attached before the delete runs" in questions[0]
    assert len(deletes) == 1

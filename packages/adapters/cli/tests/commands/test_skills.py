from __future__ import annotations

import tempfile
import uuid
from io import StringIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillCreateResponse, SkillListResponse
from anthropic.types.beta.skills import VersionCreateResponse
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
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.tenants import get_tenant
from daimon.testing.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

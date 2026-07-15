from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import cast

import httpx
import pytest
import typer
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaCloudConfig, BetaEnvironment
from anthropic.types.beta.beta_packages import BetaPackages
from anthropic.types.beta.beta_unrestricted_network import BetaUnrestrictedNetwork
from daimon.adapters.cli.commands.environments import (
    environments_archive,
    environments_create,
    environments_delete,
    environments_fork,
    environments_get,
    environments_list,
    environments_update,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.errors import StoreError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# These tests create their tenant explicitly as cli:local (the tenant
# discover_tenant derives for the CLI) and tag MA resources against it, so they
# opt out of the conftest db_session_factory autoseed to avoid a duplicate.
pytestmark = pytest.mark.no_cli_local_seed


class _FakeCli:
    local_user = "testuser"


class _FakeSettings:
    cli = _FakeCli()


def _build_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> CliRuntime:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client)
    return CliRuntime(
        settings=cast(Settings, _FakeSettings()),
        anthropic=client,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _env_json(
    *,
    env_id: str,
    name: str = "default",
    metadata: dict[str, str] | None = None,
    init_script: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    config = BetaCloudConfig(
        type="cloud",
        networking=BetaUnrestrictedNetwork(type="unrestricted"),
        packages=BetaPackages(
            type="packages",
            apt=[],
            cargo=[],
            gem=[],
            go=[],
            npm=[],
            pip=[],
        ),
        init_script=init_script,  # pyright: ignore[reportCallIssue]
        environment=environment,  # pyright: ignore[reportCallIssue]
    )
    return BetaEnvironment(
        id=env_id,
        type="environment",
        name=name,
        config=config,
        description="",
        metadata=metadata or {},
        archived_at=None,
        created_at="2026-04-22T00:00:00Z",
        updated_at="2026-04-22T00:00:00Z",
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_environments_list_returns_ma_columns(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """list returns MA-native columns (name, id, description, created_at)."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    env1 = _env_json(
        env_id="env_1",
        name="prod",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "prod"},
    )
    env2 = _env_json(
        env_id="env_2",
        name="staging",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "staging"},
    )

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([env1, env2]))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)
    await environments_list(rt=rt, console=console, as_json=False)

    out = cast(StringIO, console.file).getvalue()
    assert "prod" in out, "list output must include first environment name"
    assert "staging" in out, "list output must include second environment name"
    assert "env_1" in out or "env_2" in out, "list output must include at least one environment id"


@pytest.mark.asyncio
async def test_environments_get_resolves_via_tag(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get resolves the environment via MA tag lookup, not DB store."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    env_data = _env_json(
        env_id="env_42",
        name="mine",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "mine"},
    )

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([env_data]))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    # Should not raise StoreError
    await environments_get(rt=rt, console=console, name="mine", as_json=False)


@pytest.mark.asyncio
async def test_environments_get_not_found_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get raises StoreError when the environment is not found on MA."""
    async with db_session_factory() as s, s.begin():
        await make_tenant(s, platform="cli", workspace_id="local")

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([]))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    with pytest.raises(StoreError, match="no environment named"):
        await environments_get(rt=rt, console=console, name="ghost", as_json=False)


@pytest.mark.asyncio
async def test_environments_create_calls_sdk_create(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """create calls environments.create with spec params and metadata."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    create_bodies: list[dict[str, object]] = []

    router = MARouter()
    # The create guard lists existing tenant environments first; no collision here.
    router.add("GET", r"/v1/environments", lambda req, m: list_response([]))

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        create_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_env_json(env_id="env_new", name="new-env"))

    router.add("POST", r"/v1/environments", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "env.yaml"
    spec_path.write_text("name: new-env\nconfig:\n  type: cloud\n")

    await environments_create(rt=rt, console=console, path=spec_path)

    assert len(create_bodies) == 1, "create must POST exactly one environment"
    body = create_bodies[0]
    assert body.get("name") == "new-env", "create body must include environment name"
    md = body.get("metadata")
    assert isinstance(md, dict), "create body must include metadata dict"
    assert md.get("daimon_name") == "new-env", "metadata must tag daimon_name"  # pyright: ignore[reportUnknownMemberType]
    assert md.get("daimon_tenant") == str(tenant.id), "metadata must tag daimon_tenant"  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_environments_create_rejects_duplicate_name(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """create raises StoreError and skips the MA create when the name already exists."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    existing = _env_json(
        env_id="env_dupe",
        name="dupe",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "dupe"},
    )

    create_bodies: list[dict[str, object]] = []

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([existing]))

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        create_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_env_json(env_id="env_new", name="dupe"))

    router.add("POST", r"/v1/environments", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "env.yaml"
    spec_path.write_text("name: dupe\nconfig:\n  type: cloud\n")

    with pytest.raises(StoreError, match="already exists in this server"):
        await environments_create(rt=rt, console=console, path=spec_path)
    assert create_bodies == [], "create route must not be hit when the name collides"


@pytest.mark.asyncio
async def test_environments_fork_rejects_existing_dst(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork raises StoreError when dst already exists in the tenant (dst != src)."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    source_env = _env_json(
        env_id="env_src",
        name="source",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "source"},
    )
    existing_dst = _env_json(
        env_id="env_dst",
        name="forked",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "forked"},
    )

    create_bodies: list[dict[str, object]] = []

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([source_env, existing_dst]))

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        create_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_env_json(env_id="env_fork", name="forked"))

    router.add("POST", r"/v1/environments", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    with pytest.raises(StoreError, match="already exists in this server"):
        await environments_fork(rt=rt, console=console, src="source", dst="forked")
    assert create_bodies == [], "fork create route must not be hit when dst collides"


@pytest.mark.asyncio
async def test_environments_update_no_version_param(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """update sends no 'version' key in the request body."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    update_bodies: list[dict[str, object]] = []
    env_data = _env_json(
        env_id="env_upd",
        name="my-env",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "my-env"},
    )

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([env_data]))

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_env_json(env_id="env_upd", name="my-env"))

    router.add("POST", r"/v1/environments/env_upd", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "env.yaml"
    spec_path.write_text("name: my-env\nconfig:\n  type: cloud\n")

    await environments_update(rt=rt, console=console, name="my-env", path=spec_path)

    assert len(update_bodies) == 1, "update must POST exactly one request"
    assert "version" not in update_bodies[0], (
        f"update must not send 'version' param, got body: {update_bodies[0]}"
    )


@pytest.mark.asyncio
async def test_environments_fork_narrows_config(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork strips retrieve-only fields (init_script, environment) from config before create."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    source_env = _env_json(
        env_id="env_src",
        name="source",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "source"},
    )
    # The retrieve response includes init_script and environment (retrieve-only)
    source_full = _env_json(
        env_id="env_src",
        name="source",
        init_script="echo hello",
        environment={"FOO": "bar"},
    )

    create_bodies: list[dict[str, object]] = []

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([source_env]))
    router.add(
        "GET",
        r"/v1/environments/env_src",
        lambda req, m: httpx.Response(200, json=source_full),
    )

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        create_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_env_json(env_id="env_fork", name="forked"))

    router.add("POST", r"/v1/environments", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await environments_fork(rt=rt, console=console, src="source", dst="forked")

    assert len(create_bodies) == 1, "fork must POST exactly one create request"
    cfg = create_bodies[0].get("config")
    assert isinstance(cfg, dict), "fork body must include config dict"
    assert set(cfg.keys()) <= {  # pyright: ignore[reportUnknownArgumentType]
        "type",
        "networking",
        "packages",
    }, f"fork must only send allowed config keys, got: {set(cfg.keys())}"  # pyright: ignore[reportUnknownArgumentType]
    assert "init_script" not in cfg, "init_script is retrieve-only; must be dropped"
    assert "environment" not in cfg, "environment is retrieve-only; must be dropped"


@pytest.mark.asyncio
async def test_environments_fork_missing_dst_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork raises BadParameter when dst is None."""
    router = MARouter()
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    with pytest.raises(typer.BadParameter, match="destination name is required"):
        await environments_fork(rt=rt, console=console, src="same", dst=None)


@pytest.mark.asyncio
async def test_environments_fork_same_name_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork raises StoreError when dst equals src."""
    router = MARouter()
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    with pytest.raises(StoreError, match="conflicts with source"):
        await environments_fork(rt=rt, console=console, src="same", dst="same")


@pytest.mark.asyncio
async def test_environments_archive_calls_sdk_archive(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """archive resolves by tag then calls environments.archive."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    env_data = _env_json(
        env_id="env_arc",
        name="to-archive",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "to-archive"},
    )

    archived: list[str] = []

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([env_data]))

    def on_archive(req: httpx.Request, _m: object) -> httpx.Response:
        archived.append("env_arc")
        return httpx.Response(200, json=_env_json(env_id="env_arc", name="to-archive"))

    router.add("POST", r"/v1/environments/env_arc/archive", on_archive)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await environments_archive(rt=rt, console=console, name="to-archive", yes=True)

    assert archived == ["env_arc"], "archive must POST MA archive endpoint"


@pytest.mark.asyncio
async def test_environments_delete_success(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delete calls environments.delete on success (200 response)."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    env_data = _env_json(
        env_id="env_del",
        name="to-delete",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "to-delete"},
    )

    deleted: list[str] = []

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([env_data]))

    def on_delete(req: httpx.Request, _m: object) -> httpx.Response:
        deleted.append("env_del")
        return httpx.Response(200, json={"id": "env_del", "deleted": True})

    router.add("DELETE", r"/v1/environments/env_del", on_delete)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await environments_delete(rt=rt, console=console, name="to-delete", yes=True)

    assert deleted == ["env_del"], "delete must call MA environments.delete"


@pytest.mark.asyncio
async def test_environments_delete_409_falls_back_to_archive(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delete falls back to archive when MA returns 409."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")

    env_data = _env_json(
        env_id="env_409",
        name="conflict-env",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "conflict-env"},
    )

    archived: list[str] = []

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, m: list_response([env_data]))
    router.add(
        "DELETE",
        r"/v1/environments/env_409",
        lambda req, m: httpx.Response(
            409, json={"error": {"type": "conflict_error", "message": "in use"}}
        ),
    )

    def on_archive(req: httpx.Request, _m: object) -> httpx.Response:
        archived.append("env_409")
        return httpx.Response(200, json=_env_json(env_id="env_409", name="conflict-env"))

    router.add("POST", r"/v1/environments/env_409/archive", on_archive)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await environments_delete(rt=rt, console=console, name="conflict-env", yes=True)

    assert archived == ["env_409"], "delete 409 must fall back to calling archive"

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.environments import (
    _archive_environment_impl,
    _create_environment_impl,
    _get_environment_impl,
    _list_environments_impl,
    _update_environment_impl,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.specs import EnvironmentSpec
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant, make_thread_session
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _ma_env(**overrides: object) -> BetaEnvironment:
    base: dict[str, object] = {
        "id": "env_1",
        "type": "environment",
        "name": "demo",
        "config": {
            "type": "cloud",
            "networking": {"type": "unrestricted"},
            "packages": {
                "apt": [],
                "cargo": [],
                "gem": [],
                "go": [],
                "npm": [],
                "pip": [],
            },
        },
        "description": "",
        "created_at": "2026-04-24T00:00:00Z",
        "updated_at": "2026-04-24T00:00:00Z",
        "metadata": {},
    }
    base.update(overrides)
    return BetaEnvironment.model_validate(base)


def _runtime(
    client: AsyncAnthropic, sessionmaker: async_sessionmaker[AsyncSession] | None = None
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker or MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


async def test_list_environments_impl_returns_list(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant_id = (await make_tenant(session)).id
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda _req, _m: list_response(
            [
                _ma_env(
                    name="e1",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "e1"},
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN)
    result = await _list_environments_impl(
        _runtime(client, committing_sessionmaker), auth, page=None
    )
    assert isinstance(result, list), "should return a list"
    assert [e.name for e in result] == ["e1"], "should list the tenant's environment"


async def test_get_environment_impl_raises_not_found() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda _req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN)
    with pytest.raises(ToolError, match="not found"):
        await _get_environment_impl(_runtime(client), auth, "nope")


async def test_create_environment_impl_calls_ma_create() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=_ma_env(id="env_x", name="e").model_dump(mode="json"))

    router = MARouter()
    # The create guard lists existing tenant environments first; no collision here.
    router.add("GET", r"/v1/environments", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/environments", on_create)
    client = build_fake_anthropic(router.dispatch)

    spec = EnvironmentSpec(name="e")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _create_environment_impl(_runtime(client), auth, spec)
    assert result.name == "e", "should return the created environment name"
    assert result.id == "env_x", "should store the MA-assigned id"
    assert len(created) == 1, "should call MA create exactly once"
    assert created[0].get("metadata", {}).get("daimon_tenant") == str(tenant_id), (
        "should tag the environment with the tenant id"
    )


async def test_create_environment_sends_the_pip_list_the_caller_supplied() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=_ma_env(id="env_pip", name="e").model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/environments", on_create)
    client = build_fake_anthropic(router.dispatch)

    spec = EnvironmentSpec(
        name="e",
        config={
            "type": "cloud",
            "packages": {"type": "packages", "pip": ["numpy", "pandas", "pymc"]},
        },
    )
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _create_environment_impl(_runtime(client), auth, spec)

    assert len(created) == 1, "should call MA create exactly once"
    packages = created[0]["config"]["packages"]
    assert packages["pip"] == ["numpy", "pandas", "pymc"], (
        "outbound create request should carry the caller's full pip list"
    )
    assert packages["apt"] == [], "replace semantics should send an explicit empty apt list"
    assert packages["cargo"] == [], "replace semantics should send an explicit empty cargo list"
    assert packages["gem"] == [], "replace semantics should send an explicit empty gem list"
    assert packages["go"] == [], "replace semantics should send an explicit empty go list"
    assert packages["npm"] == [], "replace semantics should send an explicit empty npm list"


async def test_create_environment_impl_rejects_duplicate_name() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    created: list[dict[str, Any]] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.append(json_body(req))
        return httpx.Response(200, json=_ma_env(id="env_x", name="dupe").model_dump(mode="json"))

    router = MARouter()
    # An existing tenant environment with the same name forces the guard to reject.
    router.add(
        "GET",
        r"/v1/environments",
        lambda _req, _m: list_response(
            [
                _ma_env(
                    id="env_existing",
                    name="dupe",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "dupe"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/environments", on_create)
    client = build_fake_anthropic(router.dispatch)

    spec = EnvironmentSpec(name="dupe")
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="already exists in this server"):
        await _create_environment_impl(_runtime(client), auth, spec)
    assert created == [], "create route must not be hit when the name collides"


async def test_update_environment_impl_patch_only_non_none(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant_id = (await make_tenant(session)).id
    account_id = uuid.uuid4()

    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        body = _ma_env(id="env_a", name="e", description="new")
        return httpx.Response(200, json=body.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda _req, _m: list_response(
            [
                _ma_env(
                    id="env_a",
                    name="e",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "e"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/environments/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _update_environment_impl(
        _runtime(client, committing_sessionmaker),
        auth,
        name="e",
        config=None,
        description="new",
    )
    assert captured.get("description") == "new", "should forward the description"
    assert "config" not in captured, "should omit None fields"


async def test_update_environment_impl_rejects_empty_patch() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="at least one field"):
        await _update_environment_impl(
            _runtime(MagicMock()),  # type: ignore[arg-type]
            auth,
            name="e",
            config=None,
            description=None,
        )


async def test_archive_environment_impl_archives_in_ma_and_clears_its_picks(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant_id = (await make_tenant(session)).id
        for channel_id, env in (("c1", "e"), ("c2", "other")):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
                tenant_id=tenant_id,
                environment_name=env,
            )
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant_id),
            tenant_id=tenant_id,
            environment_name="e",
        )

    archived: list[str] = []

    def on_archive(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        archived.append(m.group(1))
        return httpx.Response(200, json=_ma_env(id=m.group(1)).model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda _req, _m: list_response(
            [
                _ma_env(
                    id="env_a",
                    name="e",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "e"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/environments/([^/]+)/archive", on_archive)
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=build_fake_anthropic(router.dispatch),
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )
    result = await _archive_environment_impl(runtime, auth, "e")

    assert archived == ["env_a"], "should archive the correct MA environment"
    assert result.cleared_picks == 2, "the channel pick and the workspace pick are cleared"
    assert "cleared 2 picks" in result.note, "the reply says how many picks were cleared"
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="c1"))
        is None
    ), "the channel that picked it falls through to the next tier"
    assert await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id)) is None, (
        "the workspace default pick is cleared"
    )
    other = await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="c2"))
    assert other is not None and other.environment_name == "other", (
        "a pick of another environment is untouched"
    )


async def test_update_and_archive_refuse_a_managed_environment(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A seeded environment is edited through `defaults/`, never from chat.

    A chat edit leaves the reconciler's spec hash in place, so `defaults apply`
    would skip the drifted environment forever; archiving one strands every
    seeded agent scoped onto it. Admins are refused too.
    """
    async with committing_sessionmaker.begin() as session:
        tenant_id = (await make_tenant(session)).id
    writes: list[str] = []

    def on_write(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        writes.append(req.url.path)
        return httpx.Response(500)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda _req, _m: list_response(
            [
                _ma_env(
                    id="env_seeded",
                    name="python",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "python",
                        "daimon_managed": "true",
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("POST", r"/v1/environments/([^/]+)", on_write)
    runtime = _runtime(build_fake_anthropic(router.dispatch), committing_sessionmaker)
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )

    with pytest.raises(ToolError, match="managed by defaults.*create_environment"):
        await _update_environment_impl(
            runtime, auth, name="python", config=None, description="changed"
        )
    with pytest.raises(ToolError, match="managed by defaults.*cannot archive") as refused:
        await _archive_environment_impl(runtime, auth, "python")
    assert "create_environment" not in str(refused.value), "a new one does not replace it"

    assert writes == [], "neither the update nor the archive may reach the provider"


_CLOSED_NETWORK: dict[str, object] = {
    "type": "limited",
    "allowed_hosts": [],
    "allow_mcp_servers": False,
    "allow_package_managers": False,
}


@pytest.mark.parametrize("sealed", ["c_sealed", "c_sealed;messageid=17"])
async def test_opening_or_archiving_a_sealed_channels_environment_needs_confirming(
    committing_sessionmaker: async_sessionmaker[AsyncSession], sealed: str
) -> None:
    """A sealed channel's environment opened or archived onto an open fallback is the
    same call as picking an open one there: a server admin confirms it first. A Teams
    thread sealed before channel rules still counts under its channel."""
    async with committing_sessionmaker.begin() as session:
        tenant_id = (await make_tenant(session)).id
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="c_sealed"),
            tenant_id=tenant_id,
            environment_name="closed",
        )
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant_id),
            tenant_id=tenant_id,
            environment_name="wide",
        )
        await set_access_policy(
            session,
            tenant_id=tenant_id,
            policy=TenantAccessPolicy(sealed_channel_ids=(sealed,)),
        )
    writes: list[str] = []

    def on_write(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        writes.append(req.url.path)
        return httpx.Response(200, json=_ma_env(id=m.group(1)).model_dump(mode="json"))

    def env(name: str, *, closed: bool) -> dict[str, Any]:
        config = _ma_env().config.model_dump(mode="json")
        if closed:
            config["networking"] = _CLOSED_NETWORK
        return _ma_env(
            id=f"env_{name}",
            name=name,
            config=config,
            metadata={"daimon_tenant": str(tenant_id), "daimon_name": name},
        ).model_dump(mode="json")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda _req, _m: list_response(
            [env("closed", closed=True), env("spare", closed=True), env("wide", closed=False)]
        ),
    )
    router.add("POST", r"/v1/environments/([^/]+)/archive", on_write)
    router.add("POST", r"/v1/environments/([^/]+)", on_write)
    runtime = _runtime(build_fake_anthropic(router.dispatch), committing_sessionmaker)
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )
    unrestricted: Any = {"type": "cloud", "networking": {"type": "unrestricted"}}
    a_host: Any = {"type": "cloud", "networking": {**_CLOSED_NETWORK, "allowed_hosts": ["x.io"]}}
    packages_only: Any = {"type": "cloud", "packages": {"pip": ["numpy"]}}

    for config in (unrestricted, a_host):
        with pytest.raises(
            ToolError,
            match="Channels only turns inside them read would run in the closed.*confirm_open",
        ):
            await _update_environment_impl(
                runtime, auth, name="closed", config=config, description=None
            )
    with pytest.raises(ToolError, match="the environment they would fall back to.*confirm_open"):
        await _archive_environment_impl(runtime, auth, "closed")
    assert writes == [], "nothing reaches the provider unconfirmed"

    await _update_environment_impl(
        runtime, auth, name="closed", config=packages_only, description=None
    )
    await _update_environment_impl(
        runtime, auth, name="spare", config=unrestricted, description=None
    )
    assert writes == ["/v1/environments/env_closed", "/v1/environments/env_spare"], (
        "a network left as it was, or an environment no sealed channel runs, needs no confirming"
    )
    await _update_environment_impl(
        runtime,
        auth,
        name="closed",
        config=unrestricted,
        description=None,
        confirm_open_network=True,
    )
    await _archive_environment_impl(runtime, auth, "closed", confirm_open_network=True)
    assert writes[2:] == ["/v1/environments/env_closed", "/v1/environments/env_closed/archive"], (
        "confirmed, both go through"
    )


async def test_a_sealed_thread_counts_under_the_channel_a_session_ran_it_in(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Discord thread runs in its channel's environment, which a session there places."""
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="100"),
            tenant_id=tenant.id,
            environment_name="closed",
        )
        await set_access_policy(
            session, tenant_id=tenant.id, policy=TenantAccessPolicy(sealed_channel_ids=("200",))
        )

    def env(name: str) -> dict[str, Any]:
        config = {**_ma_env().config.model_dump(mode="json"), "networking": _CLOSED_NETWORK}
        metadata = {"daimon_tenant": str(tenant.id), "daimon_name": name}
        return _ma_env(id=f"env_{name}", name=name, config=config, metadata=metadata).model_dump(
            mode="json"
        )

    router = MARouter()
    router.add("GET", r"/v1/environments", lambda _req, _m: list_response([env("closed")]))
    router.add(
        "POST",
        r"/v1/environments/([^/]+)",
        lambda _req, m: httpx.Response(200, json=_ma_env(id=m.group(1)).model_dump(mode="json")),
    )
    runtime = _runtime(build_fake_anthropic(router.dispatch), committing_sessionmaker)
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant.id, role=Role.ADMIN, is_admin=True
    )
    unrestricted: Any = {"type": "cloud", "networking": {"type": "unrestricted"}}

    await _update_environment_impl(
        runtime, auth, name="closed", config=unrestricted, description=None
    )
    async with committing_sessionmaker.begin() as session:
        await make_thread_session(session, tenant=tenant, thread_id="200", channel_id="100")
    with pytest.raises(
        ToolError,
        match="Channels only turns inside them read would run in the closed.*confirm_open",
    ):
        await _update_environment_impl(
            runtime, auth, name="closed", config=unrestricted, description=None
        )

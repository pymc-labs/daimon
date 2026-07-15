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
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import EnvironmentSpec
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response
from fastmcp.exceptions import ToolError

pytestmark = pytest.mark.asyncio


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


def _runtime(client: AsyncAnthropic) -> McpRuntime:
    return McpRuntime(
        session_factory=MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


async def test_list_environments_impl_returns_list() -> None:
    tenant_id = uuid.uuid4()
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
    result = await _list_environments_impl(_runtime(client), auth, page=None)
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


async def test_update_environment_impl_patch_only_non_none() -> None:
    tenant_id = uuid.uuid4()
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
        _runtime(client),
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


async def test_archive_environment_impl_calls_ma_archive() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

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
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _archive_environment_impl(_runtime(client), auth, "e")
    assert archived == ["env_a"], "should archive the correct MA environment"

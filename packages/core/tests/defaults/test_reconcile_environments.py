from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from anthropic.types.beta import BetaEnvironment
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.defaults.reconcile_environments import reconcile_environment
from daimon.core.defaults.report import Action
from daimon.core.specs import EnvironmentSpec
from daimon.testing.ma import EMPTY_CLOUD_CONFIG, MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http

TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _env_spec() -> EnvironmentSpec:
    return EnvironmentSpec(
        name="default", config={"type": "cloud", "packages": {"apt": ["ripgrep"]}}
    )


def _tagged_env(*, id_: str, spec: EnvironmentSpec, tenant_id: uuid.UUID) -> dict[str, Any]:
    return BetaEnvironment(
        id=id_,
        type="environment",
        name=spec.name,
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: spec.name,
        },
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")


def _router_with_envs(envs: list[dict[str, Any]]) -> MARouter:
    router = MARouter()
    router.add("GET", r"/v1/environments", lambda req, _m: list_response(envs))
    return router


def _env_response(*, id_: str) -> httpx.Response:
    return httpx.Response(
        200,
        json=BetaEnvironment(
            id=id_,
            type="environment",
            name="default",
            config=EMPTY_CLOUD_CONFIG,
            metadata={},
            description="",
            created_at="2026-04-21T00:00:00Z",
            updated_at="2026-04-21T00:00:00Z",
        ).model_dump(mode="json"),
    )


async def test_reconcile_environment_creates_when_not_on_ma() -> None:
    """No MA match → CREATE path; spec fields and metadata sent in POST body."""
    spec = _env_spec()
    router = _router_with_envs([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return _env_response(id_="env_new")

    router.add("POST", r"/v1/environments", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_environment(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.CREATED
    assert outcome.anthropic_id == "env_new"
    assert created_payload["name"] == "default"
    md = created_payload["metadata"]
    assert md["daimon_tenant"] == str(TENANT_ID)
    assert md["daimon_name"] == "default"
    assert md["daimon_managed"] == "true", (
        "reconcile must stamp daimon_managed=true so sweep distinguishes defaults from user forks"
    )
    assert "daimon_spec_hash" in md and len(md["daimon_spec_hash"]) == 16, (
        "reconcile must stamp daimon_spec_hash for L13 idempotency"
    )


async def test_reconcile_environment_updates_when_on_ma() -> None:
    """MA match found → UPDATE path; spec fields and metadata sent in POST body."""
    spec = _env_spec()
    router = _router_with_envs([_tagged_env(id_="env_1", spec=spec, tenant_id=TENANT_ID)])
    updated_payload: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        updated_payload.update(json.loads(req.content))
        return _env_response(id_="env_1")

    router.add("POST", r"/v1/environments/env_1", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_environment(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.UPDATED
    assert outcome.anthropic_id == "env_1"
    assert "name" not in updated_payload, "name must be excluded from environment update"


async def test_reconcile_environment_update_sends_explicit_empty_packages_when_spec_omits_them() -> (
    None
):
    """A spec whose config declares no packages still sends explicit empty arrays,
    so MA's field-merge replaces (clears) any packages left over from a prior apply."""
    spec = EnvironmentSpec(name="default", config={"type": "cloud"})
    prior = EnvironmentSpec(name="default", config={"type": "cloud", "packages": {"apt": ["sl"]}})
    router = _router_with_envs([_tagged_env(id_="env_1", spec=prior, tenant_id=TENANT_ID)])
    updated_payload: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        updated_payload.update(json.loads(req.content))
        return _env_response(id_="env_1")

    router.add("POST", r"/v1/environments/env_1", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_environment(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.UPDATED
    assert updated_payload["config"]["packages"] == {
        "type": "packages",
        "apt": [],
        "cargo": [],
        "gem": [],
        "go": [],
        "npm": [],
        "pip": [],
    }, "update payload carries explicit empty packages so the revert clears apt:[sl]"


async def test_reconcile_environment_archives_duplicate_not_deletes() -> None:
    """Two MA envs share the same daimon_name: the non-canonical duplicate is
    ARCHIVED (not hard-deleted). find_environments_by_daimon_tag returns the
    canonical first; the rest are duplicates. ENVC-01: a name collision must
    never physically destroy an environment."""
    spec = _env_spec()
    canonical = _tagged_env(id_="env_canonical", spec=spec, tenant_id=TENANT_ID)
    duplicate = _tagged_env(id_="env_dup", spec=spec, tenant_id=TENANT_ID)
    router = _router_with_envs([canonical, duplicate])

    archived: list[str] = []

    def on_archive(req: httpx.Request, _m: object) -> httpx.Response:
        archived.append("env_dup")
        return _env_response(id_="env_dup")

    # Register archive on the duplicate. Deliberately register a delete route
    # that fails loudly so any hard-delete attempt surfaces as a test failure.
    router.add("POST", r"/v1/environments/env_dup/archive", on_archive)
    router.add(
        "DELETE",
        r"/v1/environments/env_dup",
        lambda req, _m: httpx.Response(500, json={"error": "delete must not be called"}),
    )

    # The canonical (env_canonical) is updated, not archived/deleted.
    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        return _env_response(id_="env_canonical")

    router.add("POST", r"/v1/environments/env_canonical", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_environment(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert archived == ["env_dup"], "the duplicate env must be archived, not deleted"
    assert outcome.anthropic_id == "env_canonical", "canonical env survives the dedup"


async def test_reconcile_environment_dry_run_create() -> None:
    """dry_run=True with no MA match → CREATED action, no write calls, no anthropic_id."""
    spec = _env_spec()
    # No POST handler — router raises if reconcile tries to write.
    router = _router_with_envs([])
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_environment(client, spec, tenant_id=TENANT_ID, dry_run=True)
    assert outcome.action is Action.CREATED
    assert outcome.anthropic_id is None


async def test_reconcile_environment_dry_run_update() -> None:
    """dry_run=True with MA match → UPDATED action, no write calls, no anthropic_id."""
    spec = _env_spec()
    # No POST handler — router raises if reconcile tries to write.
    router = _router_with_envs([_tagged_env(id_="env_1", spec=spec, tenant_id=TENANT_ID)])
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_environment(client, spec, tenant_id=TENANT_ID, dry_run=True)
    assert outcome.action is Action.UPDATED
    assert outcome.anthropic_id is None

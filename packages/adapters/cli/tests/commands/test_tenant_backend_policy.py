"""Operator policy CAS consumes fresh platform facts and existing policy/audit owners."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

import httpx
import pytest
import typer
from daimon.adapters.cli.commands import tenants
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_backend import AgentBackendService
from daimon.core.config import DiscordSettings, TurnSettings
from daimon.core.errors import DaimonError, StoreError
from daimon.core.stores import access_policy, security_audit
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from pydantic import SecretStr
from rich.console import Console
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.main import get_command
from typer.testing import CliRunner

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed
GUILD, USER, ROLE = "1435062989119295640", "1533287901595435129", "1533287901595435001"


@pytest.fixture
async def policy_cli(db_preflight, db_nullpool_engine, db_clean, tmp_path):
    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    async with sm.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id=GUILD)
        account = await make_account(db, tenant=tenant)
        await make_platform_principal(
            db, tenant=tenant, account=account, platform="discord", external_id=USER
        )
    settings = SimpleNamespace(
        discord=DiscordSettings(bot_token=SecretStr("offline-discord-token")),
        turn=TurnSettings(path="mux", agent_backends=True, channel_backends=True),
    )
    rt = build_cli_runtime(sm, settings=settings)
    facts = {
        "guild": {"id": GUILD, "owner_id": "1533287901595435002"},
        "roles": [{"id": GUILD, "permissions": "0"}, {"id": ROLE, "permissions": "32"}],
        "member": {"user": {"id": USER}, "roles": [ROLE]},
        "fault": None,
        "reads": [],
    }

    def reply(request):
        assert request.method == "GET" and request.url.host == "discord.com"
        assert request.headers.get("authorization") == "Bot offline-discord-token"
        facts["reads"].append(request.url.path)
        path = request.url.path
        if facts["fault"] == "timeout":
            raise httpx.ReadTimeout("offline timeout", request=request)
        if path.endswith("/roles"):
            if facts["fault"] == "roles403":
                return httpx.Response(403, json={"message": "refused"})
            return httpx.Response(200, json=facts["roles"])
        if "/members/" in path:
            if facts["fault"] == "member404":
                return httpx.Response(404, json={"message": "removed"})
            return httpx.Response(200, json=facts["member"])
        assert path == f"/api/v10/guilds/{GUILD}"
        return httpx.Response(200, json=facts["guild"])

    transport = httpx.MockTransport(reply)
    facts["transport"] = transport
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        yield rt, tenant, account, facts, client, tmp_path / "before.json"


async def command(value, operation, **options):
    rt, _, _, _, client, path = value
    defaults = dict(output=path) if operation == "snapshot" else dict(snapshot=path)
    defaults.update(options)
    capture = StringIO()
    await tenants.tenants_backend_policy(
        rt=rt,
        console=Console(file=capture, width=240),
        operation=operation,
        platform="discord",
        external_id=GUILD,
        actor_user=USER,
        http_client=client,
        yes=True,
        **defaults,
    )
    return json.loads(capture.getvalue())


async def audit(value):
    rt, tenant, *_ = value
    async with rt.sessionmaker() as db:
        return await security_audit.list_events(db, tenant_id=tenant.id, tool_name="agent_backend")


async def test_verified_snapshot_apply_revert_preserves_other_policy_and_audits(policy_cli):
    rt, tenant, account, facts, _, path = policy_cli
    original = TenantAccessPolicy(invoker_user_ids=(USER,), dm_memory_read_only=True)
    async with rt.sessionmaker.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=tenant.id, policy=original)
    snapshot = await command(policy_cli, "snapshot")
    assert json.loads(path.read_text()) == snapshot and await audit(policy_cli) == []
    applied = await command(policy_cli, "apply", backends="anthropic,openai,gemini")
    assert applied["result"] == applied["readback"]
    assert applied["expected_revision"] == snapshot["revision"]
    assert applied["result"]["enabled_backends"] == ["anthropic", "openai", "gemini"]
    restored = await command(policy_cli, "revert", expected_revision=applied["result"]["revision"])
    assert restored["result"] == restored["readback"] == applied["before"]
    async with rt.sessionmaker() as db:
        assert await access_policy.load_access_policy(db, tenant_id=tenant.id) == original
    rows = await audit(policy_cli)
    assert len(rows) == 2
    assert all(
        r.account_id == account.id and r.operation == "allowlist" and r.outcome == "allowed"
        for r in rows
    )
    assert len([p for p in facts["reads"] if "/members/" in p]) == 5


async def test_dry_run_validates_same_cas_without_policy_audit_or_snapshot_writes(policy_cli):
    _, _, _, _, _, path = policy_cli
    await command(policy_cli, "snapshot", dry_run=True)
    assert not path.exists()
    initial = await command(policy_cli, "snapshot")
    plan = await command(policy_cli, "apply", backends="anthropic,openai", dry_run=True)
    assert plan["dry_run"] is True
    assert plan["readback"]["revision"] == initial["revision"]
    assert plan["readback"]["enabled_backends"] == ["anthropic"]
    assert plan["result"]["enabled_backends"] == ["anthropic", "openai"]
    assert await audit(policy_cli) == []


@pytest.mark.parametrize("dry_run", [False, True])
async def test_full_policy_edit_makes_saved_allowlist_revision_stale(policy_cli, dry_run):
    rt, tenant, *_ = policy_cli
    await command(policy_cli, "snapshot")
    async with rt.sessionmaker.begin() as db:
        await access_policy.set_access_policy(
            db, tenant_id=tenant.id, policy=TenantAccessPolicy(dm_memory_read_only=True)
        )
    with pytest.raises(DaimonError, match="stale revision"):
        await command(policy_cli, "apply", backends="anthropic,openai", dry_run=dry_run)
    assert await audit(policy_cli) == []


async def test_revert_requires_exact_post_apply_revision_and_never_overwrites_a_new_edit(
    policy_cli,
):
    rt, tenant, *_ = policy_cli
    await command(policy_cli, "snapshot")
    applied = await command(policy_cli, "apply", backends="anthropic,openai")
    async with rt.sessionmaker.begin() as db:
        current = await access_policy.load_access_policy(db, tenant_id=tenant.id)
        await access_policy.set_access_policy(
            db, tenant_id=tenant.id, policy=current.model_copy(update={"dm_memory_read_only": True})
        )
    with pytest.raises(DaimonError, match="stale revision"):
        await command(policy_cli, "revert", expected_revision=applied["result"]["revision"])
    assert len(await audit(policy_cli)) == 1
    assert (
        await AgentBackendService(rt.sessionmaker, enabled=True).tenant_policy(tenant.id)
    ).enabled_backends == ("anthropic", "openai")


async def test_permissions_removed_during_confirmation_refuse_before_cas(policy_cli, monkeypatch):
    _, _, _, facts, _, _ = policy_cli
    await command(policy_cli, "snapshot")
    monkeypatch.setattr(
        tenants, "confirm_or_abort", lambda *a, **kw: facts["member"].update(roles=[])
    )
    with pytest.raises(DaimonError):
        await command(policy_cli, "apply", backends="anthropic,openai")
    assert await audit(policy_cli) == []


@pytest.mark.parametrize(
    "fault",
    ["roles403", "member404", "timeout", "foreignguild", "wrongmember", "malformedrole", "noadmin"],
)
async def test_admin_attestation_failures_never_snapshot_or_mutate(policy_cli, fault):
    _, _, _, facts, _, path = policy_cli
    facts["fault"] = fault
    if fault == "foreignguild":
        facts["guild"]["id"] = "123"
    if fault == "wrongmember":
        facts["member"]["user"]["id"] = "123"
    if fault == "malformedrole":
        facts["roles"][1]["permissions"] = "unknown"
    if fault == "noadmin":
        facts["member"]["roles"] = []
    with pytest.raises(DaimonError):
        await command(policy_cli, "snapshot")
    assert not path.exists() and await audit(policy_cli) == []


@pytest.mark.parametrize(
    "backends", ["openai", "anthropic,anthropic", "anthropic,unknown", "anthropic, openai"]
)
async def test_invalid_allowlists_refuse_through_owner_validator(policy_cli, backends):
    await command(policy_cli, "snapshot")
    with pytest.raises(typer.BadParameter, match="invalid backend allowlist"):
        await command(policy_cli, "apply", backends=backends)
    assert await audit(policy_cli) == []


@pytest.mark.parametrize("field", ["tenant_id", "external_id", "platform", "extra", "revision"])
async def test_foreign_malformed_or_authority_bearing_snapshot_is_not_trusted(policy_cli, field):
    _, _, _, facts, _, path = policy_cli
    saved = await command(policy_cli, "snapshot")
    saved[field] = str(uuid.uuid4()) if field == "tenant_id" else "foreign"
    path.write_text(json.dumps(saved))
    facts["reads"].clear()
    with pytest.raises(typer.BadParameter):
        await command(policy_cli, "apply", backends="anthropic,openai")
    assert facts["reads"] == [] and await audit(policy_cli) == []


async def test_snapshot_output_is_exclusive_and_existing_snapshot_is_preserved(policy_cli):
    _, _, _, _, _, path = policy_cli
    await command(policy_cli, "snapshot")
    before = path.read_bytes()
    with pytest.raises(StoreError, match="could not create policy snapshot"):
        await command(policy_cli, "snapshot")
    assert path.read_bytes() == before and await audit(policy_cli) == []


def test_typer_exposes_only_verified_actor_and_explicit_cas_controls():
    cmd = get_command(tenants.tenants_app).commands["backend-policy"]
    options = {flag for p in cmd.params for flag in getattr(p, "opts", ())}
    assert {
        "--actor-user",
        "--snapshot",
        "--output",
        "--backends",
        "--expected-revision",
        "--dry-run",
        "--yes",
    } <= options
    assert "--is-admin" not in options and "--actor-account" not in options


async def test_concurrent_apply_commands_have_one_cas_winner_and_one_audit(policy_cli):
    await command(policy_cli, "snapshot")
    results = await asyncio.gather(
        command(policy_cli, "apply", backends="anthropic,openai"),
        command(policy_cli, "apply", backends="anthropic,gemini"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    refused = [result for result in results if isinstance(result, Exception)]
    assert len(refused) == 1 and "stale revision" in str(refused[0])
    assert len(await audit(policy_cli)) == 1


async def test_feature_off_refuses_apply_and_dry_run_without_changing_defaults(policy_cli):
    rt, tenant, *_ = policy_cli
    await command(policy_cli, "snapshot")
    rt.settings.turn.agent_backends = False
    rt.settings.turn.channel_backends = False
    for dry_run in (False, True):
        with pytest.raises(DaimonError, match="feature disabled"):
            await command(policy_cli, "apply", backends="anthropic,openai", dry_run=dry_run)
    assert (
        await AgentBackendService(rt.sessionmaker, enabled=True).tenant_policy(tenant.id)
    ).enabled_backends == ("anthropic",)
    assert await audit(policy_cli) == []


async def test_normal_typer_command_obtains_current_admin_from_default_owner_client(
    policy_cli, monkeypatch
):
    rt, tenant, account, facts, _, path = policy_cli

    @asynccontextmanager
    async def runtime(settings):
        assert settings is rt.settings
        yield rt

    original = httpx.AsyncClient
    monkeypatch.setattr(tenants, "load_settings", lambda: rt.settings)
    monkeypatch.setattr(tenants, "build_runtime", runtime)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=facts["transport"], **kwargs)
    )
    runner = CliRunner()
    saved = await asyncio.to_thread(
        runner.invoke,
        tenants.tenants_app,
        [
            "backend-policy",
            "snapshot",
            "discord",
            GUILD,
            "--actor-user",
            USER,
            "--output",
            str(path),
        ],
    )
    assert saved.exit_code == 0, saved.output
    applied = await asyncio.to_thread(
        runner.invoke,
        tenants.tenants_app,
        [
            "backend-policy",
            "apply",
            "discord",
            GUILD,
            "--actor-user",
            USER,
            "--snapshot",
            str(path),
            "--backends",
            "anthropic,openai",
            "--yes",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["result"]["enabled_backends"] == ["anthropic", "openai"]
    assert (
        await AgentBackendService(rt.sessionmaker, enabled=True).tenant_policy(tenant.id)
    ).enabled_backends == ("anthropic", "openai")
    events = await audit(policy_cli)
    assert len(events) == 1 and events[0].account_id == account.id


async def test_failed_independent_readback_retains_the_committed_cas_receipt(
    policy_cli, monkeypatch
):
    rt, tenant, _, _, client, path = policy_cli
    await command(policy_cli, "snapshot")
    original = AgentBackendService.tenant_policy
    reads = 0

    async def read(service, tenant_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise StoreError("offline independent readback unavailable")
        return await original(service, tenant_id)

    monkeypatch.setattr(AgentBackendService, "tenant_policy", read)
    capture = StringIO()
    with pytest.raises(StoreError, match="readback unavailable"):
        await tenants.tenants_backend_policy(
            rt=rt,
            console=Console(file=capture, width=240),
            operation="apply",
            platform="discord",
            external_id=GUILD,
            actor_user=USER,
            snapshot=path,
            backends="anthropic,openai",
            yes=True,
            http_client=client,
        )
    receipt = json.loads(capture.getvalue())
    actual = await original(AgentBackendService(rt.sessionmaker, enabled=True), tenant.id)
    assert receipt["result"] == actual.model_dump(mode="json")
    assert receipt["readback"] is None and len(await audit(policy_cli)) == 1

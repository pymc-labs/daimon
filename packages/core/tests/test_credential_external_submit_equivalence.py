"""Execute unchanged git-object adapters and current adapters on independent rows.

Policy decisions, consumes, locks, binding writes, shared MCP tokens, OAuth
flow rows and continuation trails use real core code and Postgres. Only chat
I/O, the MA HTTP transport and the skill import's network work are replaced.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import subprocess
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog
from cryptography.fernet import Fernet, MultiFernet
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_oauth.discovery import McpProbe
from daimon.core.posted_controls import card_for_request, card_text
from daimon.core.scope import DeploymentDefault
from daimon.core.security_audit import capture_decision
from daimon.core.stores import agent_mcp_credentials, mcp_oauth_flows
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.agent_skill_repo_credentials import list_skill_repo_credentials_for_repo
from daimon.core.stores.credential_requests import (
    consume_credential_request,
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.task_continuations import get_continuation
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic

BASE = "4b368d56fdce16934e65c4e48c51da18da4a5a7a"
URL = "https://external.example.com/mcp"
ROOT = "https://daimon.example.com"
REPO = "https://github.com/acme/skills"
KINDS = ("mcp", "skill_repo", "repo", "mcp_oauth")
POLICIES = ("open", "pinned_inside", "pinned_outside", "sealed", "pinned_sealed")


@cache
def _modules(platform, kind):
    filename = (
        "credential_oauth"
        if platform == "discord" and kind == "mcp_oauth"
        else "credential_modals"
        if platform == "discord"
        else "credential_requests"
        if platform == "teams" or kind == "mcp_oauth"
        else "credential_submissions"
    )
    name = f"daimon.adapters.{platform}.{filename}"
    current = importlib.import_module(name)
    path = f"packages/adapters/{platform}/daimon/adapters/{platform}/{filename}.py"
    source = subprocess.check_output(
        ["git", "show", f"{BASE}:{path}"], cwd=Path(__file__).resolve().parents[3], text=True
    )
    old = types.ModuleType(f"{name}_external_base")
    old.__package__ = current.__package__
    sys.modules[old.__name__] = old
    exec(compile(source, f"{BASE}:{path}", "exec"), old.__dict__)
    return old, current


async def _seed(db, platform, kind, admin, policy, shared, existing, validity):
    async with db.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id=str(uuid.uuid4().int))
        account = await make_account(session, tenant=tenant)
        if admin:
            await set_role(session, account_id=account.id, role=Role.ADMIN)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_test")
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                agent_channel_pins=(
                    {"tester": ("C_OTHER" if policy == "pinned_outside" else "C1",)}
                    if policy.startswith("pinned")
                    else {}
                ),
                sealed_channel_ids=("C1",) if "sealed" in policy else (),
            ),
        )
        row = await create_credential_request(
            session,
            token=uuid.uuid4().hex,
            kind=kind,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=account.id,
            target=f"{REPO}\nmain\nlib" if kind in ("repo", "skill_repo") else "notes",
            mcp_server_url=URL if kind in ("mcp", "mcp_oauth") else None,
            requester_platform_user_id="100000000000000001",
            channel_id="C1",
            parent_channel_id="C1",
            origin_thread_id="T1",
            posted_message_id="M1",
            platform=platform,
            expires_at=datetime.now(UTC) + timedelta(minutes=-1 if validity == "expired" else 30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="agt_test",
            target_name="tester",
            responder_name="Daimon",
            requested_work="continue",
        )
        if validity == "used":
            await consume_credential_request(session, token=row.token, now=datetime.now(UTC))
        if existing == "token" and kind == "mcp":
            await agent_mcp_credentials.upsert_credential(
                session,
                tenant_id=tenant.id,
                agent_id=agent_id,
                mcp_server_url=URL,
                encrypted_token=b"held",
            )
    servers = []
    if existing in ("name", "url"):
        servers = [
            {
                "type": "url",
                "name": "notes" if existing == "name" else "other",
                "url": "https://old.example.com/mcp" if existing == "name" else URL,
            }
        ]
    agent = ma_agent(
        id="agt_test",
        name="tester",
        mcp_servers=servers,
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_managed": str(shared).lower(),
            "fixture_reachable": str(shared == "reachable").lower(),
        },
    )
    return tenant, row, agent


def _transport(agent, row, effects, fault):
    body = agent.model_dump(mode="json")
    reads = 0
    vault_name = f"daimon-mcp:{row.account_id}:{row.agent_id}"

    def handler(request):
        path = request.url.path
        nonlocal reads
        if request.method == "GET" and path == "/v1/agents":
            reads += 1
            if fault == "race" and reads == 2:
                body["mcp_servers"] = [
                    {"type": "url", "name": "notes", "url": "https://late.example.com/mcp"}
                ]
            return httpx.Response(200, json={"data": [body], "has_more": False})
        if request.method == "GET" and path == "/v1/agents/agt_test":
            return httpx.Response(200, json=body)
        if request.method == "POST" and path == "/v1/agents/agt_test":
            if fault == "attach":
                return httpx.Response(
                    400, json={"error": {"type": "invalid_request_error", "message": "fixture"}}
                )
            update = json.loads(request.content)
            effects.append(("agent", update))
            body.update(update)
            return httpx.Response(200, json=body)
        if request.method == "GET" and path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vlt_test",
                            "type": "vault",
                            "display_name": vault_name,
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.method == "GET" and path == "/v1/vaults/vlt_test/credentials":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if request.method == "POST" and path == "/v1/vaults/vlt_test/credentials":
            if fault == "vault":
                return httpx.Response(
                    400, json={"error": {"type": "invalid_request_error", "message": "fixture"}}
                )
            auth = json.loads(request.content)["auth"]
            effects.append(("vault", auth))
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_test",
                    "type": "credential",
                    "vault_id": "vlt_test",
                    "auth": {"type": "static_bearer", "mcp_server_url": auth["mcp_server_url"]},
                },
            )
        raise AssertionError(f"Unexpected fixture HTTP request: {request.method} {path}")

    return handler


async def _run(db, module, tenant, row, agent, admin, monkeypatch, fault="none"):
    platform = module.__package__.rsplit(".", 1)[-1]
    messages, effects, audits = [], [], []
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    client = build_fake_anthropic(_transport(agent, row, effects, fault))
    http = httpx.AsyncClient()
    runtime = SimpleNamespace(
        sessionmaker=db,
        anthropic=client,
        http_client=http,
        deployment_default=DeploymentDefault(
            agent_name="tester" if agent.metadata.get("fixture_reachable") == "true" else None
        ),
        mcp_token_probe=AsyncMock(
            return_value=McpProbe(
                status_code=401 if fault == "rejected" else 200, resource_metadata_url=None
            )
        ),
        settings=SimpleNamespace(
            mcp=SimpleNamespace(
                public_url=ROOT,
                app_root_url=ROOT,
                jwt_secret=SimpleNamespace(get_secret_value=lambda: "fixture" * 8),
            ),
            github=SimpleNamespace(oauth_scopes=("repo",)),
        ),
        turn_deps=SimpleNamespace(fernet=fernet),
        # No Graph: a Teams channel admin grant names no team owner here.
        team_owners=None,
    )
    state = row.token + "oauthstatepadding"
    value = "" if fault in ("stored", "stored_denied", "public", "private") else "fixture-token"
    rejects = fault in ("rejected", "stored_denied")

    def normalize(value):
        if isinstance(value, str):
            for old, new in [
                (state, "STATE"),
                (row.token, "TOKEN"),
                (str(row.agent_id), "AGENT"),
                (str(row.tenant_id), "TENANT"),
                (str(row.account_id), "ACCOUNT"),
            ]:
                value = value.replace(old, new)
            return value
        if isinstance(value, dict):
            return {
                k: normalize(v)
                for k, v in value.items()
                if k not in ("timestamp", "tenant_id", "agent_id", "jwt_secret")
            }
        if isinstance(value, (list, tuple)):
            return tuple(normalize(v) for v in value)
        return value

    async def edit(*args, row=None, state=None, outcome=None, **kwargs):
        if row is None:
            row, state = args[:2]
        messages.append(
            (
                "card",
                state,
                card_text(
                    card_for_request(
                        row,
                        state=state,
                        outcome=outcome,
                        refusal=kwargs.get("refusal", kwargs.get("reason")),
                    )
                ),
            )
        )

    async def private(*args, **kwargs):
        messages.append(("private", kwargs.get("text", args[0] if args else "")))

    async def dispatch(*args, **kwargs):
        messages.append(("dispatch",))

    async def store_pat(*args, **kwargs):
        if fault == "storage":
            raise RuntimeError("fixture")
        effects.append(("pat",))
        return f"inline-pat:{row.agent_id}"

    with monkeypatch.context() as patch:
        # Replace only platform role lookups and external GitHub/skill work.
        for name in ("is_guild_admin", "resolve_is_admin"):
            if hasattr(module, name):
                patch.setattr(
                    module,
                    name,
                    (lambda *_: admin)
                    if name == "is_guild_admin"
                    else AsyncMock(return_value=admin),
                )
        if hasattr(module, "pat_can_access_repo"):
            patch.setattr(module, "pat_can_access_repo", AsyncMock(return_value=not rejects))
        if hasattr(module, "find_attach_mount_collision"):
            patch.setattr(module, "find_attach_mount_collision", AsyncMock(return_value=None))
        if fault == "pin_race" and hasattr(module, "request_pin_refusal"):
            patch.setattr(module, "request_pin_refusal", AsyncMock(return_value=None))
        if row.kind in ("repo", "skill_repo"):
            repo_module = importlib.import_module(
                f"daimon.adapters.{platform}."
                + (
                    "credential_repo_bind"
                    if platform == "discord"
                    else "credential_repos"
                    if platform == "teams"
                    else "agent_policy"
                )
            )
            if platform == "discord":
                patch.setattr(repo_module, "is_guild_admin", lambda *_: admin)
                patch.setattr(
                    repo_module, "pat_can_access_repo", AsyncMock(return_value=not rejects)
                )
                patch.setattr(repo_module, "store_inline_pat", store_pat)
                patch.setattr(
                    repo_module,
                    "load_agent_inline_pat",
                    AsyncMock(return_value="fixture-token" if fault.startswith("stored") else None),
                )
                patch.setattr(
                    repo_module, "is_public_repo", AsyncMock(return_value=fault == "public")
                )
            elif platform == "slack":
                patch.setattr(repo_module, "resolve_is_admin", AsyncMock(return_value=admin))
                patch.setattr(module, "store_inline_pat", store_pat)
                patch.setattr(
                    module,
                    "load_agent_inline_pat",
                    AsyncMock(return_value="fixture-token" if fault.startswith("stored") else None),
                )
                patch.setattr(module, "is_public_repo", AsyncMock(return_value=fault == "public"))
            else:
                patch.setattr(module, "store_agent_pat", store_pat)
                patch.setattr(
                    repo_module, "find_attach_mount_collision", AsyncMock(return_value=None)
                )
            patch.setattr(
                module,
                "run_skill_sync",
                AsyncMock(
                    return_value=[]
                    if fault == "empty"
                    else [
                        ResourceOutcome(
                            kind="skill",
                            name="fixture",
                            action=Action.CREATED,
                            anthropic_id="skl_test",
                        )
                    ]
                ),
            )
        if row.kind == "mcp_oauth":
            handshake = importlib.import_module("daimon.core.mcp_oauth.handshake")
            patch.setattr(handshake.secrets, "token_urlsafe", lambda *_: state)
        interaction = MagicMock()
        interaction.guild_id = int(tenant.external_id)
        interaction.user.id = int(row.requester_platform_user_id)
        interaction.user.roles = []
        interaction.response.defer = AsyncMock()
        interaction.followup.send = private
        if platform == "discord":
            patch.setattr(module, "edit_posted_card", edit)
            patch.setattr(module, "resolve_credential_target", AsyncMock(return_value=agent))
            if row.kind == "mcp_oauth":
                work = module.start_mcp_oauth_from_click(interaction, runtime=runtime, row=row)
            else:
                patch.setattr(module, "is_credential_interaction_valid", lambda *_: True)
                patch.setattr(module, "_dispatch_origin_thread", dispatch)
                form = {
                    "mcp": module.McpCredentialModal,
                    "repo": module.RepoBindModal,
                    "skill_repo": module.SkillRepoModal,
                }[row.kind](runtime=runtime, request_row=row)
                setattr(
                    form,
                    "token_input" if row.kind == "mcp" else "pat_in",
                    SimpleNamespace(value=value),
                )
                work = form.on_submit(interaction)
        elif platform == "slack":
            web = SimpleNamespace(chat_postEphemeral=private)
            patch.setattr(module, "edit_posted_card", edit)
            patch.setattr(module, "post_ephemeral", private)
            if row.kind == "mcp_oauth":
                work = module.start_mcp_oauth_from_click(
                    runtime, web, row=row, channel_id="C1", user_id=row.requester_platform_user_id
                )
            else:
                patch.setattr(module, "resolve_web_client", AsyncMock(return_value=web))
                patch.setattr(module, "_validate_submission", AsyncMock(return_value=(row, agent)))
                runner = {
                    "mcp": module.run_mcp_credential_submission,
                    "repo": module.run_repo_bind_credential_submission,
                    "skill_repo": module.run_skill_repo_credential_submission,
                }[row.kind]
                work = runner(
                    runtime,
                    team_id=tenant.external_id,
                    user_id=row.requester_platform_user_id,
                    channel_id="C1",
                    message_ts="M1",
                    token=row.token,
                    value=value,
                    dispatch_continuations=dispatch,
                )
        else:
            spawned = []
            handler = module.TeamsCredentialRequests(
                runtime=runtime,
                sender=None,
                spawn=lambda work, **_: spawned.append(work),
                dispatch=dispatch,
            )
            handler._checked = AsyncMock(return_value=(SimpleNamespace(is_admin=admin), row, None))
            handler._edit, handler._resume = edit, dispatch

            async def submit():
                response = (
                    await handler._start_oauth(row, None)
                    if row.kind == "mcp_oauth"
                    else await handler._submit(
                        SimpleNamespace(
                            value=SimpleNamespace(data={"token": row.token, "secret": value}),
                            service_url=None,
                        )
                    )
                )
                messages.append(("dialog", str(response.model_dump())))
                for task in spawned:
                    await task

            work = submit()
        with structlog.testing.capture_logs() as logs, capture_decision() as decision:
            await work
        audits.extend(logs)
    await client.close()
    await http.aclose()
    async with db() as session:
        spent = await peek_credential_request(session, token=row.token)
        continuation = await get_continuation(session, idempotency_key=row.idempotency_key)
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
        skills = await list_skill_repo_credentials_for_repo(
            session, tenant_id=row.tenant_id, repo_url=REPO
        )
        tokens = await agent_mcp_credentials.list_credentials(
            session, tenant_id=row.tenant_id, agent_id=row.agent_id
        )
        flow = await mcp_oauth_flows.get_flow(session, state=state)

    def repo_data(item):
        return (
            None
            if item is None
            else (
                item.repo_url,
                item.default_branch,
                item.ma_secret_ref,
                item.proof_kind,
                item.proof_account_id == row.account_id,
            )
        )

    trail = (
        None
        if continuation is None
        else (
            continuation.platform,
            continuation.thread_id,
            continuation.requested_work,
            continuation.reason,
            continuation.status,
        )
    )
    token_data = [
        (
            t.mcp_server_url,
            "held" if t.encrypted_token == b"held" else fernet.decrypt(t.encrypted_token).decode(),
        )
        for t in tokens
    ]
    flow_data = (
        None
        if flow is None
        else (
            flow.server_name,
            flow.mcp_server_url,
            flow.redirect_uri,
            flow.account_id == row.account_id,
            flow.code_verifier == state,
            flow.used_at,
        )
    )
    return normalize(
        (
            spent.used_at is not None,
            spent.outcome,
            trail,
            repo_data(binding),
            [(repo_data(s), s.path) for s in skills],
            token_data,
            flow_data,
            messages,
            effects,
            audits,
            dataclasses.asdict(decision),
        )
    )


@pytest.mark.parametrize("platform", ("discord", "slack", "teams"))
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("admin", (False, True), ids=("member", "admin"))
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize(
    "shared", (False, True, "reachable"), ids=("private", "managed", "reachable")
)
@pytest.mark.parametrize("existing", ("new", "name", "url", "token"))
async def test_external_submit_matches_real_base(
    db_session_factory, monkeypatch, platform, kind, admin, policy, shared, existing
):
    results = []
    for module in _modules(platform, kind):
        tenant, row, agent = await _seed(
            db_session_factory, platform, kind, admin, policy, shared, existing, "live"
        )
        results.append(
            await _run(db_session_factory, module, tenant, row, agent, admin, monkeypatch)
        )
    assert results[0] == results[1]
    if kind == "mcp" and policy == "open" and shared and not admin:
        assert results[1][0] is True
        # A private token never grants permission to attach even a new server.
        assert results[1][1] == "write_failed"
        assert results[1][8] == ()


@pytest.mark.parametrize("platform", ("discord", "slack", "teams"))
@pytest.mark.parametrize(
    "kind,fault",
    [
        ("mcp", "rejected"),
        ("mcp", "attach"),
        ("mcp", "vault"),
        ("mcp", "race"),
        ("repo", "rejected"),
        ("repo", "storage"),
        ("skill_repo", "rejected"),
        ("skill_repo", "storage"),
        ("skill_repo", "empty"),
        ("skill_repo", "attach"),
    ],
)
async def test_external_failures_match_real_base(
    db_session_factory, monkeypatch, platform, kind, fault
):
    results = []
    for module in _modules(platform, kind):
        tenant, row, agent = await _seed(
            db_session_factory, platform, kind, True, "open", False, "new", "live"
        )
        results.append(
            await _run(db_session_factory, module, tenant, row, agent, True, monkeypatch, fault)
        )
    assert results[0] == results[1]


@pytest.mark.parametrize("platform", ("discord", "slack", "teams"))
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("validity", ("used", "expired"))
async def test_external_spent_forms_match_real_base(
    db_session_factory, monkeypatch, platform, kind, validity
):
    results = []
    for module in _modules(platform, kind):
        tenant, row, agent = await _seed(
            db_session_factory, platform, kind, True, "open", False, "new", validity
        )
        results.append(
            await _run(db_session_factory, module, tenant, row, agent, True, monkeypatch)
        )
    assert results[0] == results[1]


@pytest.mark.parametrize("platform", ("discord", "slack"))
@pytest.mark.parametrize("fault", ("stored", "stored_denied", "public", "private"))
async def test_repo_credential_precedence_matches_real_base(
    db_session_factory, monkeypatch, platform, fault
):
    results = []
    for module in _modules(platform, "repo"):
        tenant, row, agent = await _seed(
            db_session_factory, platform, "repo", True, "open", False, "new", "live"
        )
        results.append(
            await _run(db_session_factory, module, tenant, row, agent, True, monkeypatch, fault)
        )
    assert results[0] == results[1]


@pytest.mark.parametrize("platform", ("discord", "slack", "teams"))
@pytest.mark.parametrize("kind", KINDS)
async def test_pin_landing_after_early_check_matches_real_base(
    db_session_factory, monkeypatch, platform, kind
):
    results = []
    for module in _modules(platform, kind):
        tenant, row, agent = await _seed(
            db_session_factory, platform, kind, False, "pinned_outside", False, "new", "live"
        )
        results.append(
            await _run(
                db_session_factory, module, tenant, row, agent, False, monkeypatch, "pin_race"
            )
        )
    assert results[0] == results[1]
    assert results[1][0] is False

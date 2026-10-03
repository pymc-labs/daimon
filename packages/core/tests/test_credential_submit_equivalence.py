"""Run base-commit adapters and current adapters against independent Postgres rows.

Only platform I/O is replaced. Policy reads, pin locks, key locks, conditional
writes, consumes, outcomes and continuation inserts use the real stores.
The fixed git object keeps the oracle unchanged as this branch evolves.
"""

from __future__ import annotations

import dataclasses
import importlib
import subprocess
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from daimon.core import credential_submit
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.credential_requests import ENV_FILE_TARGET
from daimon.core.env_file import env_name_problem
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.posted_controls import card_for_request, card_text
from daimon.core.scope import DeploymentDefault
from daimon.core.security_audit import capture_decision
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.agent_files import list_agent_files, put_agent_file
from daimon.core.stores.credential_requests import (
    consume_credential_request,
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.task_continuations import get_continuation
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant

BASE = "86cbeee1d7c9757e4c28936ee2fb1c52a68db877"
FILENAMES = {
    "discord": "credential_modals",
    "slack": "credential_submissions",
    "teams": "credential_requests",
}


@cache
def _modules(platform):
    module_name = f"daimon.adapters.{platform}.{FILENAMES[platform]}"
    current = importlib.import_module(module_name)
    path = f"packages/adapters/{platform}/daimon/adapters/{platform}/{FILENAMES[platform]}.py"
    source = subprocess.check_output(
        ["git", "show", f"{BASE}:{path}"], cwd=Path(__file__).resolve().parents[3], text=True
    )
    old = types.ModuleType(f"{module_name}_base")
    old.__package__ = current.__package__
    sys.modules[old.__name__] = old
    exec(compile(source, f"{BASE}:{path}", "exec"), old.__dict__)
    return old, current


async def _seed(
    db, platform, kind, is_admin, policy, collision, shared, target="API_KEY", validity="live"
):
    async with db.begin() as session:
        tenant = await make_tenant(session, platform=platform)
        account = await make_account(session, tenant=tenant)
        if is_admin:
            await set_role(session, account_id=account.id, role=Role.ADMIN)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_test")
        held_name = "GITHUB_TOKEN" if collision == "alias" else target
        held = None
        if collision in ("same", "alias", "stale"):
            held = await put_agent_file(
                session,
                tenant_id=tenant.id,
                agent_id=agent_id,
                key=held_name,
                content="held",
                set_by_account_id=account.id,
            )
        pins = {}
        if policy.startswith("pinned"):
            pins = {"tester": ("C_OTHER" if policy == "pinned_outside" else "C1",)}
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                agent_channel_pins=pins,
                sealed_channel_ids=("C1",) if "sealed" in policy else (),
            ),
        )
        expected = None
        if kind == "env" and held is not None and collision != "alias":
            expected = held.updated_at
            if collision == "stale":
                expected -= timedelta(seconds=1)
        row = await create_credential_request(
            session,
            token=uuid.uuid4().hex,
            kind=kind,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=account.id,
            mcp_server_url=None,
            target=ENV_FILE_TARGET if kind == "env_file" else target,
            requester_platform_user_id="100000000000000001",
            channel_id="C1",
            parent_channel_id="C1",
            origin_thread_id="T1",
            platform=None if validity == "legacy" else platform,
            expires_at=datetime.now(UTC) + timedelta(minutes=-1 if validity == "expired" else 30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="agt_test",
            target_name="tester",
            responder_name="Daimon",
            requested_work="continue",
            posted_message_id="M1",
            replaces_updated_at=expected,
        )
        if validity == "used":
            await consume_credential_request(session, token=row.token, now=datetime.now(UTC))
    agent = ma_agent(
        id="agt_test",
        name="tester",
        metadata={"daimon_tenant": str(tenant.id), "daimon_managed": "true" if shared else "false"},
    )
    return row, agent


async def _run(db, module, row, agent, is_admin, collision, monkeypatch):
    platform = module.__package__.rsplit(".", 1)[-1]
    messages = []
    audits = []
    runtime = SimpleNamespace(
        sessionmaker=db,
        deployment_default=DeploymentDefault(),
        anthropic=None,
        settings=SimpleNamespace(mcp=SimpleNamespace()),
        turn_deps=SimpleNamespace(),
    )
    target = "GH_TOKEN" if collision == "alias" else "API_KEY"
    value = f"ALPHA_KEY=first\n{target}=submitted\n" if row.kind == "env_file" else "submitted"

    async def edit(*args, row=None, state=None, outcome=None, **kwargs):
        # Teams' bound editor passes the row and state positionally.
        if row is None:
            row, state = args[:2]
        refusal = kwargs.get("refusal", kwargs.get("reason"))
        messages.append(
            (
                state,
                card_text(
                    card_for_request(
                        row,
                        state=state,
                        outcome=outcome,
                        refusal=refusal,
                        replaces=kwargs.get("replaces"),
                        refusal_lines=kwargs.get("refusal_lines", ()),
                    )
                ),
            )
        )

    async def private(*args, **kwargs):
        messages.append(("private", kwargs.get("text", args[0] if args else "")))

    async def dispatch(*args, **kwargs):
        messages.append(("dispatch", ""))

    # Inject a failed CAS after the first file entry to verify partial-write
    # rollback, including Discord's savepoint and Slack/Teams' second consume.
    write_module = credential_submit if module is _modules(platform)[1] else module
    real_write = write_module.put_agent_file_if_unchanged

    async def late_write(session, **kwargs):
        if kwargs["key"] == target:
            return None
        return await real_write(session, **kwargs)

    with monkeypatch.context() as patch:
        if collision == "late":
            patch.setattr(write_module, "put_agent_file_if_unchanged", late_write)
        if platform == "discord":
            patch.setattr(module, "is_credential_interaction_valid", lambda *_: True)
            patch.setattr(module, "is_guild_admin", lambda *_: is_admin)
            patch.setattr(module, "resolve_credential_target", AsyncMock(return_value=agent))
            patch.setattr(module, "resolve_ma_agent_for_uuid", AsyncMock(return_value=agent))
            patch.setattr(module, "edit_posted_card", edit)
            patch.setattr(module, "_dispatch_origin_thread", dispatch)
            interaction = MagicMock()
            interaction.user.id = int(row.requester_platform_user_id)
            interaction.user.roles = []
            interaction.response.defer = AsyncMock()
            interaction.followup.send = private
            form = (module.EnvFileModal if row.kind == "env_file" else module.EnvCredentialModal)(
                runtime=runtime, request_row=row
            )
            if row.kind == "env_file":
                form.file_input = SimpleNamespace(
                    values=[
                        SimpleNamespace(
                            size=len(value.encode()),
                            read=AsyncMock(return_value=value.encode()),
                        )
                    ]
                )
            else:
                form.value_input = SimpleNamespace(value=value)
            work = form.on_submit(interaction)
        elif platform == "slack":
            patch.setattr(
                module, "resolve_web_client", AsyncMock(return_value=SimpleNamespace(token="dummy"))
            )
            patch.setattr(module, "_validate_submission", AsyncMock(return_value=(row, agent)))
            patch.setattr(module, "resolve_is_admin", AsyncMock(return_value=is_admin))
            patch.setattr(module, "find_agent_by_derived_uuid", AsyncMock(return_value=agent))
            patch.setattr(module, "edit_posted_card", edit)
            patch.setattr(module, "post_ephemeral", private)
            patch.setattr(
                module,
                "fetch_slack_file",
                AsyncMock(return_value=(value.encode(), "text/plain", ".env")),
            )
            runner = (
                module.run_env_file_credential_submission
                if row.kind == "env_file"
                else module.run_env_credential_submission
            )
            work = runner(
                runtime,
                team_id="T1",
                user_id=row.requester_platform_user_id,
                channel_id="C1",
                message_ts="M1",
                token=row.token,
                dispatch_continuations=dispatch,
                **({"file_id": "F1"} if row.kind == "env_file" else {"value": value}),
            )
        else:
            spawned = []
            handler = module.TeamsCredentialRequests(
                runtime=runtime,
                sender=None,
                spawn=lambda work, **_: spawned.append(work),
                dispatch=dispatch,
            )
            handler._checked = AsyncMock(
                return_value=(SimpleNamespace(is_admin=is_admin), row, None)
            )
            handler._edit = edit
            handler._resume = dispatch
            patch.setattr(module, "find_agent_by_derived_uuid", AsyncMock(return_value=agent))

            async def submit():
                response = await handler._submit(
                    SimpleNamespace(
                        value=SimpleNamespace(data={"token": row.token, "secret": value}),
                        service_url=None,
                    )
                )
                messages.append(("dialog", str(response.model_dump()).replace(row.token, "TOKEN")))
                for task in spawned:
                    await task

            work = submit()
        with structlog.testing.capture_logs() as captured, capture_decision() as decision:
            await work
        # IDs and wall-clock timestamps differ between isolated seeded rows.
        for event in captured:
            audits.append(
                {
                    key: value
                    for key, value in event.items()
                    if key not in ("tenant_id", "agent_id", "timestamp")
                }
            )
    async with db() as session:
        spent = await peek_credential_request(session, token=row.token)
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
        continuation = await get_continuation(session, idempotency_key=row.idempotency_key)
    writes = sorted(
        (file.key, file.content, file.created_by_account_id == row.account_id) for file in files
    )
    trail = (
        None
        if continuation is None
        else (
            continuation.platform,
            continuation.thread_id,
            continuation.parent_channel_id,
            continuation.target_name,
            continuation.requested_work,
            continuation.reason,
            continuation.status,
        )
    )
    return (
        spent.used_at is not None,
        spent.outcome,
        writes,
        trail,
        messages,
        audits,
        dataclasses.asdict(decision),
    )


@pytest.mark.parametrize("platform", FILENAMES)
@pytest.mark.parametrize("kind", ("env", "env_file"))
@pytest.mark.parametrize("is_admin", (False, True), ids=("member", "admin"))
@pytest.mark.parametrize(
    "policy", ("open", "pinned_inside", "pinned_outside", "sealed", "pinned_sealed")
)
@pytest.mark.parametrize("collision", ("none", "same", "alias", "stale", "late"))
@pytest.mark.parametrize("shared", (False, True), ids=("private", "shared"))
async def test_old_and_new_env_paths(
    db_session_factory,
    monkeypatch,
    platform,
    kind,
    is_admin,
    policy,
    collision,
    shared,
):
    results = []
    for module in _modules(platform):
        row, agent = await _seed(
            db_session_factory,
            platform,
            kind,
            is_admin,
            policy,
            collision,
            shared,
            target="GH_TOKEN" if collision == "alias" else "API_KEY",
        )
        results.append(
            await _run(db_session_factory, module, row, agent, is_admin, collision, monkeypatch)
        )
    assert results[0] == results[1]


@pytest.mark.parametrize("platform", FILENAMES)
@pytest.mark.parametrize("is_admin", (False, True))
@pytest.mark.parametrize(
    "target", ("1BAD", "LD_PRELOAD", "DATABASE_URL", "OPENAI_BASE_URL", "API_KEY")
)
async def test_old_and_new_key_name_validation(
    db_session_factory, monkeypatch, platform, is_admin, target
):
    results = []
    for module in _modules(platform):
        row, agent = await _seed(
            db_session_factory, platform, "env", is_admin, "open", "none", False, target=target
        )
        results.append(
            await _run(db_session_factory, module, row, agent, is_admin, "none", monkeypatch)
        )
    assert results[0] == results[1]
    problem = env_name_problem(target, is_admin=is_admin)
    if problem is not None:
        assert results[1][0:4] == (False, None, [], None)


@pytest.mark.parametrize("platform", FILENAMES)
@pytest.mark.parametrize("kind", ("env", "env_file"))
@pytest.mark.parametrize("validity", ("legacy", "expired", "used"))
async def test_old_and_new_legacy_and_spent_forms(
    db_session_factory, monkeypatch, platform, kind, validity
):
    results = []
    for module in _modules(platform):
        row, agent = await _seed(
            db_session_factory, platform, kind, True, "open", "none", False, validity=validity
        )
        results.append(
            await _run(db_session_factory, module, row, agent, True, "none", monkeypatch)
        )
    assert results[0] == results[1]

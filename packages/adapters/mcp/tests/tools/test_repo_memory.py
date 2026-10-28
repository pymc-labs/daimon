"""The bound repo trusts its App grant; alternate reads need personal access."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import repo_memory as tools
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import GithubAppSettings, RepoMemorySettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.stores.github_access import AgentGrant, AuthorizedRepo
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


def setup_call(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SimpleNamespace, AuthIdentity, TurnOriginRow]:
    tenant, account = uuid.uuid4(), uuid.uuid4()
    origin = TurnOriginRow(
        id=uuid.uuid4(),
        tenant_id=tenant,
        account_id=account,
        chat_session_id="signed-session",
        memory_read_only=False,
        human_invocation=True,
        attended=True,
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        responder_ma_agent_id="ma_agent",
        responder_name="Agent",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )
    auth = AuthIdentity(
        tenant_id=tenant,
        account_id=account,
        role=Role.USER,
        platform="discord",
        chat_session_id="signed-session",
        chat_agent_id=derive_agent_uuid(tenant_id=tenant, ma_agent_id="ma_agent"),
        is_chat_execution=True,
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            repo_memory=RepoMemorySettings(enabled=True), github_app=GithubAppSettings()
        ),
        session_factory=MagicMock(),
        fernet=object(),
    )
    target = AuthorizedRepo(
        tenant_id=tenant,
        repo_id=17,
        owner_id=1,
        installation_id=1,
        repo_full_name="owner/repo",
        max_access="read",
        authorized_by_github_user_id=1,
        authorized_by_account_id=account,
        authorized_at=datetime.now(UTC),
        status="active",
        status_reason=None,
        version=1,
    )
    grant = AgentGrant(
        tenant_id=tenant,
        agent_id=auth.chat_agent_id,
        repo_id=17,
        baseline_access="read",
        ceiling_access="read",
        mount_path=None,
        is_working_repo=True,
        granted_by_account_id=account,
        granted_at=datetime.now(UTC),
        version=1,
    )
    monkeypatch.setattr(tools, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(tools, "load_channel_policy", AsyncMock(return_value=TenantAccessPolicy()))
    monkeypatch.setattr(
        tools,
        "effective_app_grant_view",
        AsyncMock(return_value=(((grant, target, "read"),), None, None, ())),
    )
    return runtime, auth, origin


async def test_bound_repo_needs_no_personal_link(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, auth, _ = setup_call(monkeypatch)
    personal = AsyncMock(side_effect=AssertionError("Must not request a personal GitHub link"))
    monkeypatch.setattr(tools, "requester_linked_github_user_id", personal)
    for write in (False, True):
        target = await tools.resolve_repo(runtime, auth, origin_context_id="origin", write=write)
        assert target.repo_id == 17
    assert (
        await tools.resolve_repo(runtime, auth, origin_context_id="origin", repo="OWNER/REPO")
    ).repo_id == 17
    personal.assert_not_called()


async def test_missing_working_grant_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, auth, _ = setup_call(monkeypatch)
    monkeypatch.setattr(
        tools, "effective_app_grant_view", AsyncMock(return_value=((), None, None, ()))
    )
    with pytest.raises(ToolError, match="No readable working"):
        await tools.resolve_repo(runtime, auth, origin_context_id="origin", write=True)


@pytest.mark.parametrize("write", [False, True])
async def test_routine_and_worker_refuse(monkeypatch: pytest.MonkeyPatch, write: bool) -> None:
    runtime, auth, _ = setup_call(monkeypatch)
    for identity in (replace(auth, routine_id=uuid.uuid4()), replace(auth, agent_id=uuid.uuid4())):
        with pytest.raises(ToolError, match="interactive"):
            await tools.resolve_repo(runtime, identity, origin_context_id="origin", write=write)


@pytest.mark.parametrize("operation", ["save", "delete"])
async def test_restricted_origin_blocks_both_mutations(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    runtime, auth, _ = setup_call(monkeypatch)
    policy = TenantAccessPolicy.model_validate(
        {"channel_rules": {"channel": {"readers": "inside"}}}
    )
    monkeypatch.setattr(tools, "load_channel_policy", AsyncMock(return_value=policy))
    with pytest.raises(ToolError, match="restricted"):
        await tools.repo_memory_impl(runtime, auth, operation=operation, origin_context_id="origin")


async def test_unattended_foreign_and_unknown_origins_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, auth, origin = setup_call(monkeypatch)
    for update in (
        {"attended": False},
        {"human_invocation": None},
        {"sender_foreign": True},
        {"shared_channel": True},
    ):
        monkeypatch.setattr(
            tools, "require_turn_origin", AsyncMock(return_value=origin.model_copy(update=update))
        )
        with pytest.raises(ToolError):
            await tools.resolve_repo(runtime, auth, origin_context_id="origin", write=True)
    monkeypatch.setattr(
        tools, "require_turn_origin", AsyncMock(side_effect=ToolError("invalid origin"))
    )
    with pytest.raises(ToolError, match="invalid origin"):
        await tools.resolve_repo(runtime, auth, origin_context_id="forged", write=True)


async def test_native_family_disabled_by_default() -> None:
    runtime = SimpleNamespace(settings=SimpleNamespace(repo_memory=RepoMemorySettings()))
    mcp = FastMCP()
    tools.register_repo_memory_tools(mcp, runtime)
    assert not await mcp.local_provider.list_tools()
    runtime.settings.repo_memory = RepoMemorySettings(enabled=True)
    tools.register_repo_memory_tools(mcp, runtime)
    assert {t.name for t in await mcp.local_provider.list_tools()} == {
        "repo_memory_list",
        "repo_memory_view",
        "repo_memory_save",
        "repo_memory_delete",
    }


async def test_alternate_repo_requires_personal_access(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, auth, _ = setup_call(monkeypatch)
    granted = await tools.effective_app_grant_view()
    grant, target, profile = granted[0][0]
    monkeypatch.setattr(
        tools,
        "effective_app_grant_view",
        AsyncMock(
            return_value=(
                ((grant.model_copy(update={"is_working_repo": False}), target, profile),),
                None,
                None,
                (),
            )
        ),
    )
    monkeypatch.setattr(tools, "requester_linked_github_user_id", AsyncMock(return_value=None))
    with pytest.raises(ToolError, match="linked GitHub"):
        await tools.resolve_repo(runtime, auth, origin_context_id="origin", repo="owner/repo")


@pytest.mark.parametrize("read_only", [None, True])
async def test_unknown_or_inherited_restriction_denies_both_writes(
    monkeypatch: pytest.MonkeyPatch, read_only: bool | None
) -> None:
    runtime, auth, origin = setup_call(monkeypatch)
    monkeypatch.setattr(
        tools,
        "require_turn_origin",
        AsyncMock(return_value=origin.model_copy(update={"memory_read_only": read_only})),
    )
    for operation in ("save", "delete"):
        with pytest.raises(ToolError, match="restricted"):
            await tools.repo_memory_impl(
                runtime, auth, operation=operation, origin_context_id="origin"
            )

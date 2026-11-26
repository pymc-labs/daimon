"""Every MCP tool is swept for the inside-only rule of a private channel.

A channel with limited readers, and that channel's own agent, change only from inside the
channel, admins included (`daimon.core.permissions.sealed_outside`). The caller here is a
personal admin credential with no verified origin, which is how a CLI or hub caller arrives.
The tools come from the live registry of `/mcp` and of the hub mount: a tool that is neither
swept nor listed in `EXEMPT` or `HUB_EXEMPT`, with its reason, fails
`test_every_tool_is_swept_or_exempt`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.confirmed_call import host_confirmed_call
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.middleware.call_origin import call_origin_ids
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app  # pyright: ignore[reportUnknownVariableType]
from daimon.adapters.mcp.tools import _channel_target as channel_target
from daimon.adapters.mcp.tools._authz_facts import CALL_ORIGIN
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.permissions import SEALED_REFUSAL
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.effect_recorder import database_metadata
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent_payload, ma_environment
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    build_stub_anthropic,
    list_response,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_tool_schema_snapshot import (
    _fully_configured_settings,  # pyright: ignore[reportPrivateUsage]
)

SEAL = "900000000000000001"
OPEN = "900000000000000002"
AGENT = "ag_acme"
NAME = "acme"
#: The agent that runs the caller's turn in the open channel. The sealed agent is not it.
CHAT_AGENT = "ag_chat"
CHAT_NAME = "chatty"
#: The caller's own chat session, and the one attended turn it runs in `OPEN`.
SESSION = "sweep-session"
APPROVED = uuid.uuid4()  # the person pressed Approve on every call: only the seal is left to refuse
TURN = "TURN"  # filled with the id of that turn's origin row when a call is made

#: Tools that change a channel or an agent, with the arguments of one call at the private
#: channel or its own agent. A call must raise, change nothing, and say the text in the entry.
_EXP = {"expected_ma_agent_id": AGENT}
SWEPT: dict[str, tuple[dict[str, Any], str]] = {
    "add_channel_skill": (
        {"channel_id": SEAL, "skill": "sk"},
        SEALED_REFUSAL,
    ),
    "remove_channel_skill": (
        {"channel_id": SEAL, "skill": "sk"},
        SEALED_REFUSAL,
    ),
    "archive_channel_copy": (
        {"name": NAME, "channel_id": SEAL, **_EXP},
        SEALED_REFUSAL,
    ),
    "clear_agent_default": (
        {"channel_id": SEAL},
        SEALED_REFUSAL,
    ),
    "set_agent_default": (
        {"agent_name": NAME, "channel_id": SEAL, **_EXP},
        SEALED_REFUSAL,
    ),
    "clear_channel_budget": (
        {"channel_id": SEAL},
        SEALED_REFUSAL,
    ),
    "set_channel_budget": (
        {"channel_id": SEAL, "limit_usd": "10", "window": "monthly"},
        SEALED_REFUSAL,
    ),
    "clear_channel_environment": ({"channel_id": SEAL}, SEALED_REFUSAL),
    "set_channel_environment": (
        {"environment_name": "e", "channel_id": SEAL},
        SEALED_REFUSAL,
    ),
    "clear_thread_override": (
        {"channel_id": SEAL, "thread_id": "t", "kind": "progress_plans"},
        SEALED_REFUSAL,
    ),
    "set_channel_rule": (
        {"channel_id": SEAL, "readers": "inside"},
        SEALED_REFUSAL,
    ),
    "set_agent_rule": (
        {"agent_name": NAME, "runs_in": [OPEN]},
        SEALED_REFUSAL,
    ),
    "set_agent_tool_rule": (
        {"agent_name": NAME, "tool_key": "n/t", "rule": "ask", "channel_id": SEAL},
        SEALED_REFUSAL,
    ),
    "clear_agent_tool_rule": (
        {"agent_name": NAME, "tool_key": "n/t", "channel_id": SEAL},
        SEALED_REFUSAL,
    ),
    # SEAL and OPEN both pick "shared", so the isolation view shows it and only the seal refuses.
    "archive_environment": (
        {"name": "shared", "confirm_open_network": True},
        SEALED_REFUSAL,
    ),
    "archive_agent": ({"name": NAME, **_EXP}, SEALED_REFUSAL),
    "attach_mcp_server": (
        {"agent_name": NAME, "server_name": "n", "url": "https://x.example.com/mcp", **_EXP},
        SEALED_REFUSAL,
    ),
    "detach_mcp_server": (
        {"agent_name": NAME, "server_name": "n", **_EXP},
        SEALED_REFUSAL,
    ),
    "remove_agent_key": ({"agent_name": NAME, "key": "K", **_EXP}, SEALED_REFUSAL),
    "remove_skill": ({"agent_name": NAME, "skill_id": "sk", **_EXP}, SEALED_REFUSAL),
    "set_agent_builtin_tool": (
        {"name": NAME, "tool": "bash", "enabled": True, **_EXP},
        SEALED_REFUSAL,
    ),
    "update_agent": ({"name": NAME, "description": "d", **_EXP}, SEALED_REFUSAL),
    "add_skill": (
        {"agent_name": NAME, "skill_md": "---\nname: a\ndescription: b\n---\nx", **_EXP},
        SEALED_REFUSAL,
    ),
    "set_agent_backend": ({"name": NAME, "backend": "openai"}, SEALED_REFUSAL),
    "set_agent_helpers": (
        {"agent_id": AGENT, "helpers": [], "origin_context_id": TURN},
        "No agent called",
    ),
    "set_agent_owners": (
        {"agent_id": AGENT, "owner": None, "origin_context_id": TURN},
        SEALED_REFUSAL,
    ),
    "set_agent_web_access": (
        {"agent_id": AGENT, "web": False, "origin_context_id": TURN},
        "This agent is kept to its channel.",
    ),
    "set_setup_target": (
        {"agent_id": AGENT, "origin_context_id": TURN},
        "That agent is missing here.",
    ),
    "revert_agent": (
        {"agent_id": AGENT, "version": 1, "origin_context_id": TURN},
        SEALED_REFUSAL,
    ),
    "resync_agent_skills": ({"agent_id": AGENT}, SEALED_REFUSAL),
    "save_agent_skill_files": (
        {"agent_id": AGENT, "files": {"SKILL.md": "x"}},
        SEALED_REFUSAL,
    ),
    "set_server_skill_follow": (
        {"agent_id": AGENT, "skill_id": "s", "follow": True},
        SEALED_REFUSAL,
    ),
    "apply_server_skill_update": (
        {"agent_id": AGENT, "skill_id": "s"},
        SEALED_REFUSAL,
    ),
    "set_working_repo": (
        {"repo_name": "none", "origin_context_id": TURN, "agent_name": NAME, **_EXP},
        SEALED_REFUSAL,
    ),
    "remove_repo": (
        {"repo_name": "o/r", "origin_context_id": TURN, "agent_name": NAME, **_EXP},
        SEALED_REFUSAL,
    ),
    "sync_skills": (
        {
            "url": "https://github.com/o/r",
            "branch": "main",
            "path": "s",
            "agent_name": NAME,
            **_EXP,
        },
        SEALED_REFUSAL,
    ),
    "request_agent_backend_switch": (
        {"agent_name": NAME, "target_backend": "openai", "origin_context_id": TURN},
        SEALED_REFUSAL,
    ),
}

#: Tools that refuse an outside admin in their own words, with the reason the seal cannot be the
#: refusal. Each is still a refusal that changes nothing; every other swept tool must say
#: `SEALED_REFUSAL`.
OTHER_REFUSAL: dict[str, str] = {
    "set_agent_helpers": (
        "the isolation view hides a private channel's own agent from anyone outside it, its owner "
        "included (`sees_agent` has no owner bypass), so the tool finds no such agent"
    ),
    "set_setup_target": (
        "the same hiding: a setup target is chosen among the agents the caller sees, and the tool "
        "only moves the setup thread of the caller's own open channel"
    ),
    "set_agent_web_access": (
        "an agent kept to its channel takes no web access change from anyone, inside or out "
        "(`set_agent_access` refuses `fixed_to`)"
    ),
}

#: Tools that change only the place of the caller's own verified origin: they take no channel or
#: agent argument (`test_own_place_tools_name_no_other_place`). From an origin outside a private
#: channel they can reach only that open place; at the private channel itself the caller is inside.
OWN_PLACE_ONLY: dict[str, str] = {
    "change_setting": "the workspace, agent, channel or thread of this conversation; no other place can be named",
    "request_channel_pin": "only this chat's channel can be pinned",
    "request_channel_pin_clear": "only this chat's channel can be cleared",
    "set_thread_model": "only this conversation's thread",
}

#: Tools that cannot take a verified origin, with the reason. A chat turn's credential is never a
#: personal user credential, and these tools need one (or the CLI or the hub). They run as the
#: admin's CLI credential, which has no origin and so is outside every private channel.
NO_ORIGIN: dict[str, str] = {
    "set_channel_budget": "money: needs a live platform admin on a personal user credential",
    "clear_channel_budget": "money: needs a live platform admin on a personal user credential",
    "set_agent_tool_rule": "a person's change from the CLI or the hub, never an agent in a chat",
    "clear_agent_tool_rule": "a person's change from the CLI or the hub, never an agent in a chat",
    "set_agent_backend": "in chat the switch is `request_agent_backend_switch`, which is swept",
}

_READS = "reads only; changes nothing"
_LIVE = "needs live provider (a chat platform, Microsoft or compute provider call); posts or edits content, not a setting. Its channel is checked by the isolation view"
_TURN = "starts or ends a turn or session; not a channel or agent setting. Access is the isolation view and the turn gate"
_SCHEDULED = "a routine, timer, watch or long job of the caller; content side, as web SAVE_ROUTINE. The reader rule applies when it runs"
_TENANT = "tenant-wide: people, admins, credits, allowances, promo codes, invites, support or providers; not scoped to a channel or an agent"
_OWN = "the caller's own data or preference"
_NEW = "creates or edits a tenant library item or a new agent or environment; it changes no private channel and no agent's own setup"
_MEMORY = "memory or files of the caller's own session; the isolation view hides a private channel's own agent from an outside caller"
_CARD = "needs live provider (a request card posted into a channel); the agent and channel go through the isolation view and the caller's origin"

#: Every other tool of `/mcp`, with why it is not swept.
EXEMPT: dict[str, str] = {
    "add_server_skill": _NEW,
    "approve_reach": _LIVE,
    "approve_server_skill_update": _NEW,
    "archive_my_session": _LIVE,
    "archive_thread": _LIVE,
    "ask": _TURN,
    "ask_helper": _TURN,
    "cancel_long_job": _SCHEDULED,
    "cancel_timer": _SCHEDULED,
    "cancel_turn": _TURN,
    "change_issue_state": _TENANT,
    "change_workspace_settings": _TENANT,
    "check_server_skill": _NEW,
    "compute_extend_session": _LIVE,
    "compute_get_session_status": _READS,
    "compute_launch_session": _LIVE,
    "compute_list_files": _READS,
    "compute_list_sessions": _READS,
    "compute_list_templates": _READS,
    "compute_read_file": _READS,
    "compute_run_bash": _LIVE,
    "compute_stop_session": _LIVE,
    "compute_task_result": _READS,
    "compute_task_status": _READS,
    "compute_write_file": _LIVE,
    "connect_microsoft": _LIVE,
    "continue_turn": _TURN,
    "convert": _LIVE,
    "create_agent": _NEW,
    "create_allowance": _TENANT,
    "create_attachment_upload_url": _LIVE,
    "create_environment": _NEW,
    "create_file_upload_url": _LIVE,
    "create_notebook_upload_url": _LIVE,
    "create_promo_code": _TENANT,
    "create_routine": _SCHEDULED,
    "create_thread": _LIVE,
    "create_timer": _SCHEDULED,
    "create_watch": _SCHEDULED,
    "create_web_invite": _TENANT,
    "data_context": _READS,
    "data_file": _READS,
    "data_get": _READS,
    "data_search": _READS,
    "data_sources": _READS,
    "data_sql": _READS,
    "delete_allowance": _TENANT,
    "delete_message": _LIVE,
    "delete_notebook": _LIVE,
    "delete_reply": _LIVE,
    "delete_report": _LIVE,
    "delete_routine": _SCHEDULED,
    "delete_skill": _NEW,
    "delete_thread": _LIVE,
    "delete_watch": _SCHEDULED,
    "deliver_turn_charts": _LIVE,
    "describe_agent": _READS,
    "edit_message": _LIVE,
    "explain_agent_resolution": _READS,
    "fetch_youtube_transcript": _READS,
    "forget_watch_credential": _SCHEDULED,
    "fork_agent": _NEW,
    "fork_server_skill": _NEW,
    "get_agent": _READS,
    "get_channel_budget": _READS,
    "get_cli_token": _READS,
    "get_credits_overview": _READS,
    "get_credits_spend": _READS,
    "get_environment": _READS,
    "get_feedback_summary": _READS,
    "get_issue": _READS,
    "get_long_job": _READS,
    "get_message": _READS,
    "get_monitored_conversation": _READS,
    "get_my_credits": _READS,
    "get_my_session": _READS,
    "get_ops_overview": _READS,
    "get_routine": _READS,
    "get_session": _READS,
    "get_settings": _READS,
    "get_skill": _READS,
    "get_tenant_summary": _READS,
    "get_turn_cost": _READS,
    "get_web_person": _READS,
    "github_connect": _CARD,
    "grant_admin": _TENANT,
    "hand_off_task": _TURN,
    "issue_visitor_link": _TENANT,
    "lead_intake_get": _READS,
    "lead_intake_health": _READS,
    "lead_intake_list": _READS,
    "lead_intake_read_source": _READS,
    "lead_intake_resolve": _OWN,
    "lead_intake_resume": _OWN,
    "lead_intake_submit": _OWN,
    "list_admins": _READS,
    "list_agent_keys": _READS,
    "list_agent_tool_rules": _READS,
    "list_agent_web_access": _READS,
    "list_agents": _READS,
    "list_allowances": _READS,
    "list_audit_kinds": _READS,
    "list_audit_log": _READS,
    "list_channel_budgets": _READS,
    "list_channel_skills": _READS,
    "list_channels": _READS,
    "list_coding_keys": _READS,
    "list_credentials": _READS,
    "list_environments": _READS,
    "list_events": _READS,
    "list_helpers": _READS,
    "list_issues": _READS,
    "list_library_skills": _READS,
    "list_long_jobs": _READS,
    "list_models": _READS,
    "list_monitored_activity": _READS,
    "list_my_data": _READS,
    "list_my_sessions": _READS,
    "list_notebooks": _READS,
    "list_promo_codes": _READS,
    "list_routines": _READS,
    "list_session_events": _READS,
    "list_sessions": _READS,
    "list_skills": _READS,
    "list_thread_overrides": _READS,
    "list_threads": _READS,
    "list_timers": _READS,
    "list_watches": _READS,
    "list_web_groups": _READS,
    "list_web_invites": _READS,
    "list_web_people": _READS,
    "memory_create": _MEMORY,
    "memory_delete": _MEMORY,
    "memory_str_replace": _MEMORY,
    "memory_view": _READS,
    "microsoft_get": _READS,
    "microsoft_search": _READS,
    "microsoft_send": _LIVE,
    "microsoft_write": _LIVE,
    "now": _READS,
    "parse_link": _READS,
    "pause_watch": _SCHEDULED,
    "post_github_app_install_link": _LIVE,
    "post_wizard": _LIVE,
    "preview_watch_edit": _READS,
    "publish_report": _LIVE,
    "read_channel": _READS,
    "read_library_skill": _READS,
    "read_thread": _READS,
    "redeem_promo_code": _TENANT,
    "remove_my_data": _OWN,
    "rename_thread": _LIVE,
    "replace_channel_file": _LIVE,
    "repo_memory_delete": _MEMORY,
    "repo_memory_list": _READS,
    "repo_memory_save": _MEMORY,
    "repo_memory_view": _READS,
    "report_routine_problem": _SCHEDULED,
    "request_agent_key": _CARD,
    "request_github_access": _CARD,
    "request_mcp_oauth": _CARD,
    "request_mcp_token": _CARD,
    "request_provider_allowlist": _TENANT,
    "request_watch_credential": _SCHEDULED,
    "restore_library_skill_version": _NEW,
    "resume_watch": _SCHEDULED,
    "revoke_admin": _TENANT,
    "revoke_promo_code": _TENANT,
    "revoke_web_link": _TENANT,
    "rotate_watch_key": _SCHEDULED,
    "run_routine": _SCHEDULED,
    "save_library_skill_files": _NEW,
    "search_messages": _READS,
    "self_delete_file": _MEMORY,
    "self_list_files": _READS,
    "self_read_file": _READS,
    "self_write_file": _MEMORY,
    "send_direct_message": _LIVE,
    "send_message": _LIVE,
    "set_allowance_amount": _TENANT,
    "set_display_identity": _LIVE,
    "set_never_tag": _OWN,
    "set_routine_sandbox_tools": _SCHEDULED,
    "set_web_invite_files": _TENANT,
    "share_secret_with": _LIVE,
    "show_watch": _READS,
    "start_fresh_task": _TURN,
    "start_long_job": _SCHEDULED,
    "start_turn": _TURN,
    "support_channel_get": _READS,
    "support_channel_set": _TENANT,
    "support_limit_clear": _TENANT,
    "support_limit_list": _READS,
    "support_limit_set": _TENANT,
    "support_menu_get": _READS,
    "support_menu_set": _TENANT,
    "support_menu_use": _TENANT,
    "support_requests_list": _READS,
    "support_requests_settle": _TENANT,
    "update_environment": _NEW,
    "update_plan": _OWN,
    "update_routine": _SCHEDULED,
    "update_watch": _SCHEDULED,
    "update_web_person": _TENANT,
    "wait_for_helpers": _TURN,
    "web_fetch": _READS,
    "where_am_i": _READS,
}

#: The hub mount's tools. It forwards turns to a Daimon and lists sessions; it changes no setting.
_HUB = "forwards a turn or reads a session of a Daimon; changes no channel or agent setting"
HUB_EXEMPT: dict[str, str] = {
    "ask": _HUB,
    "continue_turn": _HUB,
    "data_context": _READS,
    "data_file": _READS,
    "data_get": _READS,
    "data_search": _READS,
    "data_sources": _READS,
    "data_sql": _READS,
    "describe_daimon": _HUB,
    "get_session": _HUB,
    "list_daimons": _HUB,
    "list_events": _HUB,
    "list_library_skills": _READS,
    "list_my_sessions": _HUB,
    "read_library_skill": _READS,
    "start_turn": _HUB,
}


@dataclass
class _Tools:
    by_name: dict[str, Any]
    auth: AuthIdentity
    writes: list[str]
    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    runtime: McpRuntime
    origin_id: str


class _Ctx:
    """What a registered tool reads of its request: the identity the middleware would set."""

    def __init__(self, auth: AuthIdentity) -> None:
        self._auth = auth

    async def get_state(self, key: str) -> object:
        return self._auth if key == "auth" else None

    async def set_state(self, *_: object, **__: object) -> None:
        return None


@asynccontextmanager
async def _server(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_origin: bool = True,
) -> AsyncIterator[_Tools]:
    """A workspace with a private channel and its own agent, and an admin outside it.

    The admin runs an attended chat turn in the open channel `OPEN`, so its verified origin is
    that channel: every origin check passes, and only the seal can refuse a call. A tool in
    `NO_ORIGIN` gets the admin's CLI credential instead, which has no origin.
    """
    tenant = await make_tenant(db_session, platform="discord")
    account = await make_account(db_session, tenant=tenant)
    await set_role(db_session, account.id, Role.ADMIN)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={SEAL: ChannelRule(readers="own", writers="own")},
            agent_rules={NAME: AgentRule(runs_in=(SEAL,))},
        ),
    )
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id=OPEN,
        thread_id=OPEN,
        responder_ma_agent_id=CHAT_AGENT,
        responder_name=CHAT_NAME,
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.ADMIN,
        sender_foreign=False,
        shared_channel=False,
        expires_at=now + timedelta(minutes=10),
        now=now,
        chat_session_id=SESSION,
    )
    # The admin owns the private channel's agent, so isolation does not hide it: only the seal can refuse.
    await agent_owners.record_creator(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT),
        account_id=account.id,
    )
    await db_session.commit()
    metadata = {"daimon_tenant": str(tenant.id)}
    agents = [
        ma_agent_payload(
            id=AGENT,
            name=NAME,
            metadata={**metadata, "daimon_name": NAME, "daimon_account": str(account.id)},
            mcp_servers=[{"type": "url", "name": "n", "url": "https://x.example.com/mcp"}],
        ),
        ma_agent_payload(
            id=CHAT_AGENT, name=CHAT_NAME, metadata={**metadata, "daimon_name": CHAT_NAME}
        ),
    ]
    for channel_id in (SEAL, OPEN):
        await set_fields(
            db_session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel_id),
            tenant_id=tenant.id,
            environment_name="shared",
        )
    await db_session.commit()
    shared = ma_environment(
        id="env_shared",
        name="shared",
        tenant_id=tenant.id,
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "shared"},
    )
    writes: list[str] = []
    router = MARouter()
    router.add_environment_list(shared)
    router.add("GET", r"/v1/agents$", lambda _r, _m: list_response(agents))
    for each in agents:
        router.add(
            "GET",
            rf"/v1/agents/{each['id']}",
            lambda _r, _m, each=each: httpx.Response(200, json=each),
        )

    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            writes.append(f"{request.method} {request.url.path}")
        return router.dispatch(request)

    async def visible(_runtime: McpRuntime, _auth: AuthIdentity, channel_id: str) -> str:
        return channel_id

    monkeypatch.setattr(channel_target, "resolve_visible_channel", visible)
    client = build_fake_anthropic(dispatch)
    settings = _fully_configured_settings()
    app = create_mcp_app(
        settings=settings,
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=client,
    )
    tools = {t.name: t for t in await app.state.mcp.local_provider.list_tools()}  # type: ignore[attr-defined]
    chat = {
        "chat_session_id": SESSION,
        "chat_agent_id": derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=CHAT_AGENT),
        "is_chat_execution": True,
        "confirmed_call_id": APPROVED,
    }
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id=tenant.external_id,
        platform_user_id="u-admin",
        is_admin=True,
        is_platform_admin=True,
        token_kind=None if with_origin else "cli",
        **(chat if with_origin else {}),  # pyright: ignore[reportArgumentType]
    )
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=client,
        settings=settings,
        deployment_default=DeploymentDefault(environment_name=None),
    )
    # What `CallOriginMiddleware` sets for each call: the channel of the caller's attended turn.
    inside: frozenset[str] = (
        await call_origin_ids(runtime, auth, str(origin.id)) if with_origin else frozenset()
    )
    assert with_origin == (OPEN in inside) and SEAL not in inside, "the caller is outside SEAL"
    token = CALL_ORIGIN.set(inside)
    approved = host_confirmed_call.set(APPROVED)
    try:
        yield _Tools(tools, auth, writes, sessionmaker, tenant.id, runtime, str(origin.id))
    finally:
        host_confirmed_call.reset(approved)
        CALL_ORIGIN.reset(token)


async def _tenant_rows(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, list[str]]:
    """Every row of every table that carries the workspace's id, so any change shows."""
    rows: dict[str, list[str]] = {}
    for table in database_metadata().sorted_tables:
        if "tenant_id" not in table.c:
            continue
        found = await session.execute(select(table).where(table.c.tenant_id == tenant_id))
        rows[table.name] = sorted(repr(tuple(row)) for row in found)
    return rows


@pytest.mark.parametrize("name", sorted(SWEPT))
async def test_an_admin_outside_changes_no_setting_of_a_private_channel_or_its_agent(
    name: str,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments, words = SWEPT[name]
    async with _server(
        db_session, sessionmaker, monkeypatch, with_origin=name not in NO_ORIGIN
    ) as server:
        arguments = {k: server.origin_id if v == TURN else v for k, v in arguments.items()}
        async with sessionmaker() as session:
            before = await _tenant_rows(session, server.tenant_id)
        with pytest.raises(ToolError) as refused:
            await server.by_name[name].fn(ctx=_Ctx(server.auth), **arguments)
        async with sessionmaker() as session:
            after = await _tenant_rows(session, server.tenant_id)
    print("OBSERVED", name, json.dumps(str(refused.value))[:200])
    assert words in str(refused.value), (name, str(refused.value))
    assert server.writes == [], "nothing reached the provider"
    assert before == after, "no row of the workspace changed"


async def test_every_tool_is_swept_or_exempt(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_fake_anthropic(lambda _r: httpx.Response(404)),
    )
    live = {t.name for t in await app.state.mcp.local_provider.list_tools()}  # type: ignore[attr-defined]
    hub = build_hub_app(
        platform="discord",
        runtime=McpRuntime(
            session_factory=sessionmaker,
            client=build_stub_anthropic(),
            settings=_fully_configured_settings(),
            deployment_default=DeploymentDefault(environment_name=None),
        ),
        auth=StaticTokenVerifier(tokens={}),
        billing_config=None,
    )
    hub_live = {t.name for t in await hub.local_provider.list_tools()}
    assert not set(SWEPT) & set(EXEMPT), "a tool is swept or exempt, not both"
    assert not set(OWN_PLACE_ONLY) & (set(SWEPT) | set(EXEMPT))
    assert live == set(SWEPT) | set(EXEMPT) | set(OWN_PLACE_ONLY), (
        "new tools: sweep them or list them in EXEMPT with a reason",
        sorted(live - set(SWEPT) - set(EXEMPT)),
        "gone tools",
        sorted((set(SWEPT) | set(EXEMPT) | set(OWN_PLACE_ONLY)) - live),
    )
    assert hub_live == set(HUB_EXEMPT), sorted(hub_live ^ set(HUB_EXEMPT))
    assert all(EXEMPT.values()) and all(HUB_EXEMPT.values())


#: Arguments that would name a place other than the caller's own.
_PLACE_ARGUMENTS = frozenset(
    {"channel_id", "thread_id", "agent_id", "agent_name", "name", "channel", "agent", "target"}
)


async def test_own_place_tools_name_no_other_place(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_fake_anthropic(lambda _r: httpx.Response(404)),
    )
    tools = {t.name: t for t in await app.state.mcp.local_provider.list_tools()}  # type: ignore[attr-defined]
    for name in OWN_PLACE_ONLY:
        assert not set(tools[name].parameters["properties"]) & _PLACE_ARGUMENTS, name


def test_every_other_refusal_and_origin_free_tool_is_swept() -> None:
    for name in OTHER_REFUSAL:
        assert SWEPT[name][1] != SEALED_REFUSAL, (
            f"{name} says the sealed words; drop it from OTHER_REFUSAL"
        )
    for name, (_arguments, words) in SWEPT.items():
        assert (words != SEALED_REFUSAL) == (name in OTHER_REFUSAL), (
            f"{name}: a refusal in other words needs its reason in OTHER_REFUSAL"
        )
    assert set(NO_ORIGIN) <= set(SWEPT)

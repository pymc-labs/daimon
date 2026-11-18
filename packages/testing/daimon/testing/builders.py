"""Builders for rows and payloads that many suites spell out the same way.

Each takes the callee's own keyword names and fills the ones nearly every test repeats;
a test states only what its scenario varies. A builder that takes a tenant accepts the
row or its id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from anthropic.types.beta.file_metadata import FileMetadata
from anthropic.types.beta.skill_list_response import SkillListResponse
from daimon.core.config import McpSettings
from daimon.core.defaults.provisioning import ProvisionResult, provision_tenant
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.domain import (
    AgentFileRow,
    Platform,
    RoutineRow,
    TenantRow,
    ThreadSessionRow,
)
from daimon.core.stores.routines import create_routine
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _tenant_id(tenant: TenantRow | uuid.UUID) -> uuid.UUID:
    return tenant if isinstance(tenant, uuid.UUID) else tenant.id


async def new_routine(
    session: AsyncSession,
    tenant: TenantRow | uuid.UUID,
    *,
    created_by_user_id: str | None = None,
    agent_id: str = "agent_a",
    agent_name: str = "daimon",
    cron_expr: str = "* * * * *",
    timezone_: str = "UTC",
    trigger_message: str = "run",
    **fields: Any,  # noqa: ANN401 - forwards create_routine's keywords
) -> RoutineRow:
    """`create_routine` for a routine that runs every minute, unless the test says otherwise."""
    return await create_routine(
        session,
        tenant_id=_tenant_id(tenant),
        created_by_user_id=created_by_user_id,
        agent_id=agent_id,
        agent_name=agent_name,
        cron_expr=cron_expr,
        timezone_=timezone_,
        trigger_message=trigger_message,
        **fields,
    )


async def new_agent_file(
    session: AsyncSession,
    tenant: TenantRow | uuid.UUID,
    *,
    agent_id: uuid.UUID,
    key: str,
    content: str,
    set_by_account_id: uuid.UUID | None = None,
) -> AgentFileRow:
    """`put_agent_file`, written by no acting person unless `set_by_account_id` says who."""
    return await put_agent_file(
        session,
        tenant_id=_tenant_id(tenant),
        agent_id=agent_id,
        key=key,
        content=content,
        set_by_account_id=set_by_account_id,
    )


async def new_thread_session(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    thread_id: str,
    ma_session_id: str,
    platform: str = "discord",
    account_id: uuid.UUID | None = None,
    **fields: Any,  # noqa: ANN401 - forwards create_thread_session's keywords
) -> ThreadSessionRow:
    """`create_thread_session` on Discord for a fresh account unless the test says otherwise."""
    return await create_thread_session(
        session,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        account_id=account_id if account_id is not None else uuid.uuid4(),
        ma_session_id=ma_session_id,
        **fields,
    )


async def provision_workspace(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    platform: Platform = "discord",
    signup_credit: Decimal = Decimal("0"),
) -> ProvisionResult:
    """`provision_tenant` for a Discord workspace with no signup credit."""
    return await provision_tenant(
        sessionmaker, platform=platform, workspace_id=workspace_id, signup_credit=signup_credit
    )


async def admit_chat(
    deps: TurnDeps,
    tenant: TenantRow | uuid.UUID,
    *,
    platform: str = "discord",
    external_user_id: str = "user-1",
    channel_id: str = "chan-1",
    requester_org: RequesterOrg = RequesterOrg.HOST,
    origin: str = "chat",
    **fields: Any,  # noqa: ANN401 - forwards admit's keywords
) -> Admission:
    """`admit` a chat message from a host-organisation member in a Discord channel."""
    return await admit(
        deps,
        tenant_id=_tenant_id(tenant),
        platform=platform,
        external_user_id=external_user_id,
        channel_id=channel_id,
        requester_org=requester_org,
        origin=origin,
        **fields,
    )


def plain_turn_deps(
    anthropic: Any,  # noqa: ANN401 - tests pass fakes and mocks
    sessionmaker: async_sessionmaker[AsyncSession],
    **fields: Any,  # noqa: ANN401 - forwards TurnDeps' fields
) -> TurnDeps:
    """A `TurnDeps` with no billing, GitHub, fernet or public URL, open tool safety, unless set."""
    defaults: dict[str, Any] = {
        "deployment_default": DeploymentDefault(),
        "resolver_cache": new_resolver_cache(),
        "defaults_root": Path("/nonexistent"),
        "mcp": McpSettings(),
        "billing_config": None,
        "markup": Decimal("1.0"),
        "fernet": None,
        "github_fallback_pat": None,
        "github_app_id": None,
        "github_app_private_key": None,
        "public_url": None,
        "tool_safety": OPEN_TOOL_SAFETY,
    }
    return TurnDeps(anthropic=anthropic, sessionmaker=sessionmaker, **(defaults | fields))


_SKILL_STAMP = "2026-04-21T00:00:00Z"


def skill_listed(**fields: Any) -> SkillListResponse:  # noqa: ANN401 - SkillListResponse's fields
    """A custom skill as the skills list returns it; `fields` override the stamp and kind."""
    defaults: dict[str, Any] = {
        "type": "custom",
        "source": "custom",
        "latest_version": "1",
        "created_at": _SKILL_STAMP,
        "updated_at": _SKILL_STAMP,
    }
    return SkillListResponse(**(defaults | fields))


_FILE_STAMP = datetime(2026, 10, 9, tzinfo=UTC)


def file_meta(**fields: Any) -> FileMetadata:  # noqa: ANN401 - FileMetadata's fields
    """A downloadable text file as the Files API returns it; `fields` override the defaults."""
    defaults: dict[str, Any] = {
        "type": "file",
        "downloadable": True,
        "mime_type": "text/plain",
        "created_at": _FILE_STAMP,
    }
    return FileMetadata(**(defaults | fields))

"""Permanent native-admin behaviour after channel grant removal."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Surface, authorize, build_subject
from daimon.core.channel_budget_notice import BudgetNotice, notice_recipients
from daimon.core.config import load_settings
from daimon.core.support_routing import support_recipient_tiers


def test_former_holder_has_no_policy_rights() -> None:
    member = build_subject(is_admin=False, platform_user_id="user")
    admin = build_subject(is_admin=True, platform_user_id="admin")
    policy = TenantAccessPolicy.model_validate({"agent_channel_pins": {"helper": ["channel"]}})
    agent = AgentRef.of("helper")
    assert not authorize(
        policy,
        subject=member,
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id="channel"),
    )
    assert not authorize(policy, subject=member, action=Action.SET_AGENT_TOOL_RULE)
    assert authorize(policy, subject=admin, action=Action.SET_AGENT_TOOL_RULE)
    assert not authorize(
        policy, subject=member, action=Action.RUN_AGENT, surface=Surface.DM, agent=agent
    )
    assert not authorize(
        policy, subject=member, action=Action.RUN_AGENT, surface=Surface.HUB, agent=agent
    )
    assert authorize(
        policy, subject=admin, action=Action.RUN_AGENT, surface=Surface.HUB, agent=agent
    )


async def test_budget_notice_and_support_use_native_admin_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core import channel_budget_notice

    sessionmaker = MagicMock(return_value=AsyncMock())
    admins = AsyncMock(return_value=["server-admin"])
    monkeypatch.setattr(channel_budget_notice, "list_platform_user_ids", admins)
    notice = BudgetNotice(
        tenant_id=uuid.uuid4(),
        workspace_id="guild",
        platform="discord",
        channel_id="channel",
        recipient_ids=(),
        limit_line="$5.00",
        monthly=False,
        budget_id=uuid.uuid4(),
        window_key="window",
    )
    assert await notice_recipients(sessionmaker, notice) == ("server-admin",)
    assert (
        await support_recipient_tiers(
            sessionmaker,
            tenant_id=notice.tenant_id,
            platform="discord",
            channel_id="channel",
            requester_id="requester",
        )
        == ()
    )


def test_stale_environment_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "true")
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    settings = load_settings(_env_file=None)
    assert not hasattr(settings, "channel_admins")

"""Approvals need a live native admin; the CLI operator is the one other actor allowed, by type.

A chat user who is not an admin, an agent key and an `admin_for`-true actor with no chat
identity are all refused."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
)
from daimon.core.features import service
from daimon.core.features.service import FeatureActor, OperatorActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.settings_panel import chat_actor
from daimon.core.stores.domain import ChangeSurface
from daimon.core.stores.security_audit import SecurityAuditRow, list_events
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

REPO = Path(__file__).resolve().parents[4]


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


async def _yes(_: DelegatedAction | None) -> bool:
    return True


def _operator(tenant_id: uuid.UUID, reason: str = "unattended bridge") -> OperatorActor:
    return OperatorActor(tenant_id, None, "cli", None, _yes, operator="ops", reason=reason)


async def _audit_rows(session: AsyncSession, tenant_id: uuid.UUID) -> list[SecurityAuditRow]:
    return await list_events(session, tenant_id=tenant_id, tool_name="approval_gate")


async def _set(
    factory: async_sessionmaker[AsyncSession], actor: FeatureActor, tier: str = "workspace"
) -> None:
    await change_feature(
        factory,
        _settings(),
        actor,
        key="approvals",
        tier=tier,  # pyright: ignore[reportArgumentType]
        value="off",
        place=FeaturePlace.of(actor.tenant_id, "discord", "c1" if tier == "channel" else None),
    )


async def test_the_operator_sets_approvals_and_the_audit_row_names_who_and_why(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _set(db_session_factory, _operator(tenant.id))
    await _set(db_session_factory, _operator(tenant.id, "client channel"), tier="channel")
    assert [(e.platform_user_id, e.reason) for e in await _audit_rows(db_session, tenant.id)] == [
        ("operator:ops", "server:inherit->off (unattended bridge)"),
        ("operator:ops", "c1:inherit->off (client channel)"),
    ], "each operator change is audited with operator, old and new value and the reason"


@pytest.mark.parametrize(
    "actor_kwargs",
    [
        {"native_user_id": None},  # a chat user with no admin standing in chat
        {"native_user_id": None, "via_agent_key": True},
        {"native_user_id": "u1", "via_agent_key": True},  # an agent key never counts as an admin
    ],
    ids=["no chat identity", "agent key", "agent key with a chat id"],
)
async def test_other_actors_are_refused_even_when_admin_for_says_yes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    actor_kwargs: dict[str, object],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = FeatureActor(tenant.id, None, "web", admin_for=_yes, **actor_kwargs)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ChannelChangeRefused):
        await _set(db_session_factory, actor)
    assert not await _audit_rows(db_session, tenant.id), "a refused change writes no audit row"


@pytest.mark.parametrize(
    ("operator", "reason", "surface", "via_agent_key", "match"),
    [
        ("", "why", "cli", False, "reason"),
        ("ops", "  ", "cli", False, "reason"),
        ("ops", "why", "cli", True, "agent key"),
        ("ops", "why", "chat", False, "CLI"),
    ],
)
def test_an_operator_actor_needs_a_name_a_reason_and_the_cli(
    operator: str, reason: str, surface: ChangeSurface, via_agent_key: bool, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        OperatorActor(
            uuid.uuid4(),
            None,
            surface,
            None,
            _yes,
            via_agent_key=via_agent_key,
            operator=operator,
            reason=reason,
        )


def test_only_the_cli_entrypoint_builds_an_operator_actor() -> None:
    """The operator path is reachable from the CLI alone; any other module naming the type
    could let a chat or MCP path approve its own writes."""
    service = REPO / "packages/core/daimon/core/features/service.py"
    users = {
        path.relative_to(REPO).as_posix()
        for path in (REPO / "packages").rglob("*.py")
        if "tests" not in path.parts and path != service and "OperatorActor" in path.read_text()
    }
    assert users == {"packages/adapters/cli/daimon/adapters/cli/commands/features.py"}, users


async def test_a_settings_panel_actor_is_refused_unless_the_live_check_says_admin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`chat_actor` (the chat settings panels) vouches via `admin_for`, which approvals ignore."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    asked: list[str] = []

    async def not_admin(*_: object, user_id: str, **__: object) -> Subject:
        asked.append(user_id)
        return Subject(is_admin=False, platform_user_id=user_id)

    monkeypatch.setattr(service, "approval_admin", not_admin)
    actor = await chat_actor(
        db_session_factory, tenant_id=tenant.id, platform="discord", user_id="u1", admin_for=_yes
    )
    with pytest.raises(ChannelChangeRefused):
        await _set(db_session_factory, actor)
    assert asked == ["u1"], "the live native check ran for the panel user"
    assert not await _audit_rows(db_session, tenant.id), (
        "a refused panel change writes no audit row"
    )

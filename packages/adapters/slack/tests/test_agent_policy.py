"""Tests for daimon.adapters.slack.agent_policy.

The policy table itself is unit-tested in core; what is proved here is the
shell half: which facts get read, in which order, what the caller is told, and
that the two wrappers over it (the panel gate and the chat credential gate)
inherit exactly that.

Real Postgres (`db_session_factory`) for reachability — written as real
propagation rows, never by patching the predicate — plus the transport-level
Anthropic fake and a real `AsyncWebClient` under `aioresponses`.
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import yarl
from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.agent_policy import (
    AGENT_GONE_MESSAGE,
    MANAGED_AGENT_MESSAGE,
    NEEDS_ADMIN_SPEC_MESSAGE,
    SHARED_AGENT_MESSAGE,
    refusal_message,
    refuse_unless_allowed,
    refuse_unless_allowed_for_agent_name,
)
from daimon.adapters.slack.credential_submissions import refuse_if_shared_and_not_admin_for_request
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.testing.factories import make_tenant, make_tenant_config
from daimon.testing.ma import MARouter, build_fake_anthropic, make_fake_ma_handler
from daimon.testing.ma_models import ma_agent
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM_ID = "T_POLICY_TESTS"
_USER_ID = "U_POLICY_TEST"
_CHANNEL_ID = "C_POLICY_TEST"
_AGENT_NAME = "policy-agent"
_MA_AGENT_ID = f"agent_{'p' * 24}"

_SLACK_API_BASE = "https://slack.com/api"
_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")
_EPHEMERAL_KEY = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))


def _users_info_payload(*, is_admin: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "user": {
            "id": _USER_ID,
            "name": "admin" if is_admin else "member",
            "is_admin": is_admin,
            "is_owner": False,
            "is_primary_owner": False,
        },
    }


def _build_runtime(
    db_factory: async_sessionmaker[AsyncSession] | None,
    *,
    handler: Any = None,
) -> SlackRuntime:
    """A runtime whose sessionmaker raises when `db_factory` is None.

    A test that passes None is asserting the decision never reaches the
    database; the raise turns that into a failure rather than a silent pass.
    """

    def _fail(*args: object, **kwargs: object) -> AsyncSession:
        raise AssertionError("the database must not be read for this decision")

    return SlackRuntime(
        settings=MagicMock(),
        anthropic=build_fake_anthropic(handler if handler is not None else make_fake_ma_handler()),
        sessionmaker=db_factory if db_factory is not None else MagicMock(side_effect=_fail),  # pyright: ignore[reportArgumentType]
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


def _agent_list_handler(
    *, tenant_id: uuid.UUID, managed: bool = False, calls: list[str] | None = None
) -> Any:
    """Serve one agent from `GET /v1/agents`, optionally counting every call."""
    router = MARouter()
    router.add_agent_list(
        ma_agent(
            id=_MA_AGENT_ID,
            name=_AGENT_NAME,
            tenant_id=tenant_id,
            metadata={MA_METADATA_KEY_MANAGED: "true"} if managed else None,
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        return router.dispatch(request)

    return handler


def _ephemeral_texts(mock: AioResponsesMock) -> list[str]:
    return [
        str((kwargs.get("json") or {}).get("text", ""))
        for _, kwargs in mock.requests.get(_EPHEMERAL_KEY, [])
    ]


async def test_attachment_write_allows_an_admin_without_reading_the_target(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An admin attaching to any agent is allowed before any fact is fetched.

    Binding a repo or replacing a key on the workspace's built-in agent is the
    first-run onboarding step; the policy table answers `allow` for an admin
    against every combination of facts, so neither the MA fetch nor the
    reachability read should happen.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await make_tenant_config(session, tenant=tenant, agent_name=_AGENT_NAME, mode="agent")
        await session.commit()

    ma_calls: list[str] = []
    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=True), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        runtime = _build_runtime(
            None, handler=_agent_list_handler(tenant_id=tenant.id, calls=ma_calls)
        )

        refused = await refuse_unless_allowed(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            operation="repo_bind",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is False, "an admin must never be refused an attachment write"
        assert ma_calls == [], "the admin short-circuit must precede the target fetch"
        assert _EPHEMERAL_KEY not in mock.requests, "an allowed call posts nothing"


async def test_spec_edit_refuses_a_managed_agent_even_for_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A defaults-managed agent's spec is off-limits to everyone.

    A panel edit never stamps the reconciler's spec hash, so an admin bypass
    here would leave permanent drift from the shipped defaults.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=True), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        runtime = _build_runtime(
            None, handler=_agent_list_handler(tenant_id=tenant.id, managed=True)
        )

        refused = await refuse_unless_allowed_for_agent_name(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            operation="agent_spec_edit",
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is True, "the built-in agent's spec is not editable by an admin either"
        assert _ephemeral_texts(mock) == [MANAGED_AGENT_MESSAGE], (
            "the managed-agent refusal is the shared copy, character for character"
        )


async def test_spec_edit_refuses_a_member_when_the_target_answers_somewhere(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A member editing the agent the workspace currently depends on is refused."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await make_tenant_config(session, tenant=tenant, agent_name=_AGENT_NAME, mode="agent")
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=False), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        runtime = _build_runtime(
            db_session_factory, handler=_agent_list_handler(tenant_id=tenant.id)
        )

        refused = await refuse_unless_allowed_for_agent_name(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            operation="agent_spec_edit",
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is True, "a reachable agent's spec needs an admin"
        assert _ephemeral_texts(mock) == [NEEDS_ADMIN_SPEC_MESSAGE], (
            "the refusal names the permission the caller lacks, once"
        )


async def test_spec_edit_allows_a_member_when_the_target_answers_nowhere(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unreachable agent has no live gate to defend, so a member may edit it."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=False), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        runtime = _build_runtime(
            db_session_factory, handler=_agent_list_handler(tenant_id=tenant.id)
        )

        refused = await refuse_unless_allowed_for_agent_name(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            operation="agent_spec_edit",
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is False, "an unshared agent stays member-editable"
        assert _EPHEMERAL_KEY not in mock.requests, "a pass-through posts nothing"


async def test_refuse_unless_allowed_refuses_when_the_agent_id_is_gone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A derived uuid that resolves to no live agent fails closed.

    The request row was minted against an agent that has since been archived;
    there is nowhere correct left to write.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=False), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        runtime = _build_runtime(
            db_session_factory, handler=_agent_list_handler(tenant_id=tenant.id)
        )

        refused = await refuse_unless_allowed(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            operation="repo_bind",
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is True, "an unresolvable target fails closed"
        assert _ephemeral_texts(mock) == [AGENT_GONE_MESSAGE], (
            "the caller is told the agent is gone, not that they lack permission"
        )


async def test_posted_token_write_allows_a_member_without_fetching_the_target(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A single-use posted-token contribution needs no admin and no facts.

    It writes one value on one key of one agent and overwrites nothing, so the
    policy table allows it outright — and the shell must not pay for reads the
    decision cannot use.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await make_tenant_config(session, tenant=tenant, agent_name=_AGENT_NAME, mode="agent")
        await session.commit()

    ma_calls: list[str] = []
    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=False), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        runtime = _build_runtime(
            None, handler=_agent_list_handler(tenant_id=tenant.id, calls=ma_calls)
        )

        refused = await refuse_unless_allowed(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            operation="key_add",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is False, "a posted-token write is open to every member"
        assert ma_calls == [], "a decision that cannot turn on the target reads nothing"


async def test_credential_gate_reachable_limb_uses_decide_operation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The chat repo-bind gate refuses exactly what the policy table refuses.

    Same facts, same answer, same copy as the panel's attachment gate — the
    wrapper contributes the I/O and the ephemeral, never a second rule.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await make_tenant_config(session, tenant=tenant, agent_name=_AGENT_NAME, mode="agent")
        await session.commit()

    assert (
        decide_operation(
            "repo_bind",
            is_admin=False,
            target=TargetFacts(is_daimon_managed=False, is_reachable_in_tenant=True),
        )
        == "needs_admin"
    ), "the table is what decides a member's repo bind against a reachable agent"

    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=False), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        runtime = _build_runtime(
            db_session_factory, handler=_agent_list_handler(tenant_id=tenant.id)
        )

        refused = await refuse_if_shared_and_not_admin_for_request(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is True, "a member may not re-point a shared agent's working repo"
        assert _ephemeral_texts(mock) == [SHARED_AGENT_MESSAGE], (
            "the chat gate and the panel gate say the same thing"
        )


def test_refusal_copy_differs_by_operation_family() -> None:
    """The two families refuse for different reasons and say different things.

    Pinned here rather than in each shell test so a copy edit breaks one
    assertion instead of five.
    """
    assert refusal_message("agent_spec_edit", "managed_agent") == MANAGED_AGENT_MESSAGE, (
        "a spec edit against the built-in agent points at forking"
    )
    assert refusal_message("agent_spec_edit", "needs_admin") == NEEDS_ADMIN_SPEC_MESSAGE, (
        "a spec edit on a reachable agent names the permission"
    )
    assert refusal_message("repo_bind", "managed_agent") == SHARED_AGENT_MESSAGE, (
        "attachment writes carry one string for both refused outcomes"
    )
    assert refusal_message("key_remove", "needs_admin") == SHARED_AGENT_MESSAGE, (
        "attachment writes carry one string for both refused outcomes"
    )

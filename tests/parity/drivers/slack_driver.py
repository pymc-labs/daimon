"""SlackDriver -- the Slack half of the PlatformDriver Protocol.

Builds a SlackRuntime + SlackApp from an injected MARouter-backed fake
AsyncAnthropic and drives a turn via the REAL `SlackApp._handle_app_mention`
entry point (D-02) -- dedup, per-event token resolve, Slack Connect gate,
tenant resolve, THEN `_orchestrate` -> `_run_thread_turn` -> `run_turn`.
`create_session` is boundary-stubbed (mirrors the Discord driver and the
existing `_make_orchestrate_app` test pattern); `resolve_agent`,
`resolve_environment`, `build_context_xml`, and `run_turn` itself all run
for real against the injected MARouter + an aioresponses-intercepted
AsyncWebClient (D-01) -- no `unittest.mock.patch` of `run_turn`.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from unittest.mock import MagicMock, patch

import httpx
from aioresponses import aioresponses as AioResponsesMock
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup import write as slack_write
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime, build_turn_deps
from daimon.core.defaults.provisioning import teardown_slack_install
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.purge import AccountPurgeResult
from daimon.core.purge import purge_account as core_purge_account
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.ma import MARouter, build_fake_anthropic
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SLACK_API_BASE = "https://slack.com/api"
_POST_JSON_METHODS: tuple[str, ...] = (
    "auth.test",
    "chat.postMessage",
    "chat.update",
    "chat.postEphemeral",
)
_REACTIONS_ADD_PATTERN = re.compile(r"https://slack\.com/api/reactions\.add.*")
_CONVERSATIONS_REPLIES_PATTERN = re.compile(r"https://slack\.com/api/conversations\.replies.*")

_BALANCE_BLOCKED_TEXT = (
    "This workspace's daimon credit is depleted. An admin can top up with `/billing`."
)
_CAP_BLOCKED_TEXT = (
    "Monthly usage cap reached for this workspace. "
    "An admin can adjust the cap with `/billing` (when available)."
)


def _register_slack_defaults(mock: AioResponsesMock) -> None:
    """Canned ok=True responses for the Slack Web API methods a turn touches.

    Mirrors `packages/adapters/slack/tests/conftest.py`'s
    `_register_slack_defaults` -- duplicated here rather than imported since
    that module lives under a package's `tests/` tree, not on the
    `daimon.testing` import path.
    """
    for method in _POST_JSON_METHODS:
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/{method}",
            payload={"ok": True, "ts": "1000000000.000001", "channel": "C_PARITY"},
            repeat=True,
        )
    mock.post(_REACTIONS_ADD_PATTERN, payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        _CONVERSATIONS_REPLIES_PATTERN,
        payload={"ok": True, "messages": [], "has_more": False},
        repeat=True,
    )


def _extract_posted_texts(mock: AioResponsesMock) -> list[str]:
    """Every `text` body posted via chat.postMessage / chat.update, in
    request order (blocked-copy and the final agent reply both land here)."""
    posted: list[str] = []
    for (_method, url), reqs in mock.requests.items():
        if str(url) not in (
            f"{_SLACK_API_BASE}/chat.postMessage",
            f"{_SLACK_API_BASE}/chat.update",
        ):
            continue
        for req in reqs:
            # aioresponses ships no type stubs -- `req.kwargs` is Unknown;
            # narrow to Any explicitly rather than let it propagate.
            req_kwargs = cast(dict[str, Any], req.kwargs)  # pyright: ignore[reportUnknownMemberType]
            body = cast(dict[str, Any], req_kwargs.get("json") or {})
            text = cast(Any, body.get("text"))
            if isinstance(text, str):
                posted.append(text)
    return posted


def _make_fake_session(
    *, session_id: str, agent_id: str, model_id: str
) -> BetaManagedAgentsSession:
    now = datetime.now(UTC)
    return BetaManagedAgentsSession(
        outcome_evaluations=[],
        id=session_id,
        agent=BetaManagedAgentsSessionAgent(
            id=agent_id,
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id=model_id),
            name="test-agent",
            skills=[],
            tools=[],
            type="agent",
            version=1,
        ),
        created_at=now,
        environment_id="env_parity_test",
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at=now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
    )


@dataclass
class SlackDriver:
    """Drives turns through `SlackApp._handle_app_mention`, the real Slack entry point."""

    param_id: str = "slack"

    def _make_runtime(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        *,
        fernet_key: str,
        billing_config: object | None = None,
    ) -> SlackRuntime:
        settings = MagicMock()
        settings.crypto.keys = (SecretStr(fernet_key),)
        settings.slack.max_concurrent_turns_per_tenant = 100
        settings.slack.history_page_limit = 200
        settings.mcp.public_url = None
        settings.mcp.app_root_url = None
        settings.mcp.jwt_secret = None
        settings.defaults_root = MagicMock()
        settings.billing.markup = Decimal("1.0")
        anthropic = build_fake_anthropic(router.dispatch)
        deployment_default = DeploymentDefault(agent_name="test-agent", environment_name="test-env")
        resolver_cache = new_resolver_cache()
        turn_deps = build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=billing_config,  # pyright: ignore[reportArgumentType]  # test-injected BillingConfig | None
        )
        return SlackRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=sessionmaker,
            billing_config=billing_config,  # pyright: ignore[reportArgumentType]  # test-injected BillingConfig | None
            http_client=MagicMock(spec=httpx.AsyncClient),
            resolver_cache=resolver_cache,
            turn_deps=turn_deps,
            deployment_default=deployment_default,
        )

    async def dispatch_turn(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        text: str,
        billing_config: object | None = None,
        thread_ts: str | None = None,
    ) -> list[str]:
        """`thread_ts`, when given, targets an existing thread (e.g. a
        pre-seeded live `thread_sessions` row) instead of starting a fresh
        one -- the event's own `ts`/`event_ts` stay unique per dispatch
        (mirrors a real follow-up mention), only `thread_ts` is pinned so
        `_orchestrate`'s `thread_id = event.get("thread_ts") or event.get("ts")`
        resolves to the caller-chosen id. Not part of the `PlatformDriver`
        Protocol (Discord's `channel_id` already doubles as the thread id) --
        this is Slack-only, keyword-only, and defaults to None so every
        existing call site is unaffected.
        """
        fernet_key = Fernet.generate_key().decode()
        fernet = build_multifernet((fernet_key,))
        async with sessionmaker() as s:
            await upsert_slack_bot_token(
                s, team_id=workspace_id, encrypted_token=encrypt_token(fernet, "xoxb-parity-test")
            )
            await s.commit()

        runtime = self._make_runtime(
            sessionmaker, router, fernet_key=fernet_key, billing_config=billing_config
        )
        app = SlackApp(runtime=runtime)

        event_ts = f"{time.time():.6f}"
        event: dict[str, Any] = {
            "type": "app_mention",
            "channel": channel_id,
            "event_ts": event_ts,
            "ts": event_ts,
            "user": user_id,
            "text": f"<@U_BOT> {text}",
        }
        if thread_ts is not None:
            event["thread_ts"] = thread_ts

        with (
            AioResponsesMock() as mock,
            patch("daimon.core.turn.prepare.create_session") as mock_create_session,
        ):
            _register_slack_defaults(mock)
            mock_create_session.return_value = _make_fake_session(
                session_id="sess_parity_test",
                agent_id="ag_parity_test",
                model_id="claude-sonnet-4-6",
            )
            await app._handle_app_mention(event, team_id=workspace_id)  # pyright: ignore[reportPrivateUsage]
            posted = _extract_posted_texts(mock)
        return posted

    def expected_blocked_text(self, kind: Literal["balance", "cap"]) -> str:
        return _BALANCE_BLOCKED_TEXT if kind == "balance" else _CAP_BLOCKED_TEXT

    async def delete_agent(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        name: str,
    ) -> None:
        runtime = self._make_runtime(
            sessionmaker, router, fernet_key=Fernet.generate_key().decode()
        )
        await slack_write.delete_agent(runtime, tenant_id=tenant_id, name=name)

    async def fork_agent(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        source_name: str,
        new_name: str,
        account_id: uuid.UUID,
    ) -> None:
        runtime = self._make_runtime(
            sessionmaker, router, fernet_key=Fernet.generate_key().decode()
        )
        await slack_write.fork_agent(
            runtime,
            tenant_id=tenant_id,
            source_name=source_name,
            new_name=new_name,
            account_id=account_id,
        )

    async def purge_account(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        account_id: uuid.UUID,
    ) -> AccountPurgeResult:
        return await core_purge_account(
            sm=sessionmaker,
            account_id=account_id,
            anthropic=build_fake_anthropic(router.dispatch),
        )

    async def uninstall(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        await teardown_slack_install(sessionmaker, team_id=workspace_id, now=datetime.now(UTC))

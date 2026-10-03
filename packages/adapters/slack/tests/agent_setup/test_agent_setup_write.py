"""Tests for live setup-panel write helpers."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup import write as write_mod
from daimon.adapters.slack.agent_setup.write import (
    load_agent_inline_pat,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.scope import DeploymentDefault
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _runtime_for_inline_pat(
    *,
    sessionmaker: Any,
    fernet_key: str | None,
    fallback_pat: str | None = None,
) -> SlackRuntime:
    """Build a SlackRuntime with just enough settings for load_agent_inline_pat."""
    settings = MagicMock()
    settings.crypto.keys = (
        (MagicMock(get_secret_value=lambda: fernet_key),) if fernet_key is not None else ()
    )
    settings.github.oauth_scopes = ("repo", "read:user")
    settings.github.fallback_pat = (
        MagicMock(get_secret_value=lambda: fallback_pat) if fallback_pat is not None else None
    )
    return SlackRuntime(
        settings=settings,
        anthropic=MagicMock(),
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        deployment_default=DeploymentDefault(),
    )


async def test_load_agent_inline_pat_returns_none_when_crypto_unconfigured() -> None:
    """No crypto keys -> no inline PAT could exist; the sessionmaker must never
    be touched (calling _build_runtime_fernet unconditionally would raise)."""
    agent_id = uuid.uuid4()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("sessionmaker must not be called when crypto is unconfigured")

    runtime = _runtime_for_inline_pat(sessionmaker=_boom, fernet_key=None)

    result = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert result is None, "no crypto keys configured -> no inline PAT can exist"


async def test_load_agent_inline_pat_returns_stored_pat_for_agent_that_has_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Round-trips through store_inline_pat -> load_agent_inline_pat, decrypted."""
    await make_tenant(db_session, platform="slack", workspace_id="T_INLINE_PAT_LOAD")

    fernet_key = Fernet.generate_key().decode()
    plaintext = "ghp_slack_inline_pat_load_9999"
    runtime = _runtime_for_inline_pat(sessionmaker=db_session_factory, fernet_key=fernet_key)

    agent_id = uuid.uuid4()
    await write_mod.store_inline_pat(
        runtime,
        account_id=uuid.uuid4(),
        agent_id=agent_id,
        plaintext_pat=plaintext,
    )

    result = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert result == plaintext, "load_agent_inline_pat must decrypt and return the exact stored PAT"


async def test_load_agent_inline_pat_returns_none_for_agent_with_no_token_even_with_fallback_configured(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """allow_service_default=False must be honored: an agent with no stored
    token of its own gets None, never the deployment's shared fallback PAT —
    this is the check that stops the shared public-read token from being
    treated as the agent's own credential."""
    await make_tenant(db_session, platform="slack", workspace_id="T_INLINE_PAT_NO_FALLBACK")

    fernet_key = Fernet.generate_key().decode()
    runtime = _runtime_for_inline_pat(
        sessionmaker=db_session_factory,
        fernet_key=fernet_key,
        fallback_pat="ghp_shared_operator_fallback",
    )

    result = await load_agent_inline_pat(runtime, agent_id=uuid.uuid4())
    assert result is None, (
        "an agent with no stored token must resolve to None, not the shared fallback PAT"
    )

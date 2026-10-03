"""Inline PAT helpers for Slack."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_credentials import (
    build_multifernet,
    get_pat,
    upsert_credential_encrypted,
)
from daimon.core.stores.agent_github_binding import set_agent_github_binding

if TYPE_CHECKING:
    from cryptography.fernet import MultiFernet

_log = structlog.get_logger()


def _build_runtime_fernet(runtime: SlackRuntime) -> MultiFernet:
    """Build a MultiFernet from ``runtime.settings.crypto.keys``."""
    keys = tuple(secret.get_secret_value() for secret in runtime.settings.crypto.keys)
    return build_multifernet(keys)


async def load_agent_inline_pat(runtime: SlackRuntime, *, agent_id: uuid.UUID) -> str | None:
    """Return the inline PAT ``core/sessions.py`` would resolve for ``agent_id``, or None.

    This is the exact credential ``resolve_clone_token``'s ``per_agent_pat``
    short-circuit will use to clone any repo later bound to this agent,
    regardless of which repo that PAT was originally verified against — which
    is why a caller binding a *different* repo must re-verify this value
    against it before writing a binding.

    Returns None (no crypto call at all) when ``runtime.settings.crypto.keys``
    is empty: no inline PAT can exist on a deployment that has never
    configured crypto (storing one requires crypto too, via
    ``store_inline_pat``), and calling ``_build_runtime_fernet`` unconditionally
    here would raise ``ValueError`` on such a deployment, breaking a
    previously-working bind path.

    Passes the service-default opt-in as disabled and no fallback token
    explicitly: the operator's shared service PAT must never be treated as
    this agent's own clone credential — letting it through here would gate
    every re-verification on whether the shared public-read token covers the
    repo, breaking private App-covered binds.
    """
    if not runtime.settings.crypto.keys:
        return None
    return await get_pat(
        principal_id=agent_id,
        agent_id=agent_id,
        sessionmaker=runtime.sessionmaker,
        fernet=_build_runtime_fernet(runtime),
        allow_service_default=False,
        fallback_pat=None,
    )


async def store_inline_pat(
    runtime: SlackRuntime,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    plaintext_pat: str,
) -> str:
    """Fernet-encrypt the inline PAT and write a per-agent credential overlay.

    Stored under principal_id=agent_id (per-agent principal). Connecting
    GitHub for Agent A does not let Agent B resolve the PAT.

    Returns the ``ma_secret_ref`` string used by ``agent_repo_binding.set_binding``.
    """
    fernet = _build_runtime_fernet(runtime)
    await upsert_credential_encrypted(
        sessionmaker=runtime.sessionmaker,
        fernet=fernet,
        principal_id=agent_id,
        github_login="(inline-pat)",
        plaintext_token=plaintext_pat,
        scopes=tuple(runtime.settings.github.oauth_scopes),
    )
    async with runtime.sessionmaker.begin() as session:
        await set_agent_github_binding(session, agent_id=agent_id, principal_id=agent_id)
    _log.info("repo_auth.pat_stored")
    return f"inline-pat:{agent_id}"

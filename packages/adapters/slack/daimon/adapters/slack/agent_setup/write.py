"""Write-path helpers for the /agent-setup panel (Slack adapter).

Ports the Discord agent_setup write + scope_default logic, swapping the
platform-specific runtime type and audit-display helper for their Slack
equivalents. All core saga/store calls are reused UNCHANGED. No cross-adapter
imports (import-linter contract).

GitHub OAuth platform-keying (RESEARCH A3): the state row is keyed to
``platform="slack"`` with a string Slack user ID (e.g. ``"U123456"``). The
callback resolver routes via the ``platform`` column — this prevents
cross-platform state reuse (T-83-09).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core import agent_lifecycle
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import (
    build_multifernet,
    get_pat,
    upsert_credential_encrypted,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_agent
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.scoped_config_write import clear_agent_references

if TYPE_CHECKING:
    from cryptography.fernet import MultiFernet

_log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def owner_repo_from_url(url: str) -> str:
    """Extract canonical ``owner/repo`` from a GitHub URL or short path.

    Must stay byte-identical to
    ``daimon.core.stores.agent_repo_binding._normalize_owner_repo`` — a probe
    run against a differently-canonicalized string would verify a different
    repo than the one the binding actually records.
    """
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


# ---------------------------------------------------------------------------
# Agent mutation wrappers (port of Discord write.py)
# ---------------------------------------------------------------------------


def _build_runtime_fernet(runtime: SlackRuntime) -> MultiFernet:
    """Build a MultiFernet from ``runtime.settings.crypto.keys``."""
    keys = tuple(secret.get_secret_value() for secret in runtime.settings.crypto.keys)
    return build_multifernet(keys)


async def delete_agent(runtime: SlackRuntime, *, tenant_id: uuid.UUID, name: str) -> None:
    """Archive the MA agent matching ``name`` under the given tenant.

    Channel and workspace scope rows naming the agent are cleared as part of the
    delete, so turn resolution falls through the cascade instead of resolving to
    a deleted agent.
    """
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise DaimonError(f"No agent named *{name}* found.")
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        # Server-side refusal, and on Slack the ONLY one: RosterEntry carries no
        # managed flag, so the panel offers Delete on a seeded agent exactly as
        # it does on a user agent, and the click branch checks only admin.
        # Archiving here would take the deployment's built-in agent and its
        # memory store with it.
        raise DaimonError(
            f"*{name}* is a built-in agent and cannot be deleted. "
            "Fork it first, then delete the fork."
        )
    await archive_agent(runtime.anthropic, agent.id, scope=resource_scope(tenant_id=str(tenant_id)))
    await agent_lifecycle.archive_memory_store_best_effort(
        anthropic=runtime.anthropic,
        sessionmaker=runtime.sessionmaker,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        log_context={"tenant_id": str(tenant_id), "agent_name": name, "ma_agent_id": agent.id},
    )
    # After the MA archive, never before: a failure here leaves an archived
    # agent with stale scope rows rather than a live agent with cleared ones.
    # Deliberately unguarded — a failure must reach the action's error boundary
    # rather than degrade silently.
    async with runtime.sessionmaker.begin() as session:
        await clear_agent_references(session, tenant_id=tenant_id, agent_name=name)


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

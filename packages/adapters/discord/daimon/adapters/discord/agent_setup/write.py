"""Agent creation and inline PAT helpers for Discord."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_identity import queue_agent_face
from daimon.core.constants import ALLOWED_MODEL_IDS
from daimon.core.defaults.ma_index import (
    find_agents_by_daimon_tag,
)
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import AgentNameCollision, SpecError
from daimon.core.github_credentials import (
    build_multifernet,
    get_pat,
    upsert_credential_encrypted,
)
from daimon.core.specs import (
    AgentSpec,
)
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from pydantic import ValidationError

_log = structlog.get_logger()

if TYPE_CHECKING:
    from cryptography.fernet import MultiFernet


def validate_model_id(model: str) -> str | None:
    """Return an error message if `model` is not in the allow-list; None if valid.

    UX-25-03: Discord modals cannot contain Select components, so we validate
    free-text input at submit time against ALLOWED_MODEL_IDS.
    """
    if model not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        return f"Model `{model}` is not allowed. Choose one of: {allowed}"
    return None


async def load_agent_inline_pat(runtime: DiscordRuntime, *, agent_id: uuid.UUID) -> str | None:
    """Return the inline PAT `core/sessions.py` would resolve for `agent_id`, or None.

    This is the exact credential `resolve_clone_token`'s `per_agent_pat`
    short-circuit will use to clone any repo later bound to this agent,
    regardless of which repo that PAT was originally verified against — which
    is why a caller binding a *different* repo must re-verify this value
    against it before writing a binding.

    Returns None (no crypto call at all) when `runtime.settings.crypto.keys`
    is empty: no inline PAT can exist on a deployment that has never
    configured crypto (storing one requires crypto too, via
    `store_inline_pat`), and calling `_build_runtime_fernet` unconditionally
    here would raise `ValueError` on such a deployment, breaking a
    previously-working bind path.

    Passes `allow_service_default=False` and no `fallback_pat` explicitly:
    the operator's shared service PAT must never be treated as this agent's
    own clone credential — letting it through here would gate
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
    )


async def create_blank_agent(
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    name: str,
    system: str | None,
    model: str,
    account_id: uuid.UUID,
) -> ResourceOutcome:
    """Build a blank AgentSpec from modal fields, reconcile, and start its face.

    Tenant-scoped name uniqueness: rejects if `name` already exists anywhere in
    this tenant, regardless of owner. Agent names are tenant-wide identity —
    reconcile dedup and the resolver key on (tenant, name) only.
    """
    collisions = await find_agents_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if collisions:
        raise AgentNameCollision(
            f"This server already has an agent named **{name}**. Pick a different name."
        )
    try:
        spec = AgentSpec.model_validate({"name": name, "model": model, "system": system})
    except ValidationError as err:
        raise SpecError(f"Spec validation failed: {err}") from err
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    outcome = await reconcile_agent(
        runtime.anthropic,
        spec,
        tenant_id=tenant_id,
        dry_run=False,
        account_id=account_id,
        public_url=public_url,
        # New agents created from the panel are user-owned, NOT seeded
        # resources — managed=True would stamp daimon_managed=true and make
        # them sweep-eligible, so the next defaults apply (every boot/deploy)
        # archives them because they aren't in the seeded spec list.
        managed=False,
    )
    if outcome.anthropic_id is not None:
        queue_agent_face(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            agent_name=name,
            metadata=None,  # managed=False above: only the default name can make it built-in
            default_agent_name=runtime.deployment_default.agent_name,
        )
    return outcome


def _build_runtime_fernet(runtime: DiscordRuntime) -> MultiFernet:
    """Build a MultiFernet from `runtime.settings.crypto.keys`."""
    keys = tuple(secret.get_secret_value() for secret in runtime.settings.crypto.keys)
    return build_multifernet(keys)


async def store_inline_pat(
    runtime: DiscordRuntime,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    plaintext_pat: str,
) -> str:
    """Fernet-encrypt the inline PAT and write a per-agent credential overlay.

    The credential is stored under principal_id=agent_id (per-agent principal),
    and an agent_github_binding(agent_id -> agent_id) overlay row is written so that
    get_pat(agent_id=agent_id) resolves exactly this credential. Connecting GitHub for
    Agent A does not let Agent B resolve the PAT.

    Returns the `ma_secret_ref` string used by `agent_repo_binding.set_binding`.
    """
    fernet = _build_runtime_fernet(runtime)
    # Write the per-agent credential (principal = agent_id) and the overlay binding.
    # After this, get_pat(agent_id=agent_id) resolves exactly this token.
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

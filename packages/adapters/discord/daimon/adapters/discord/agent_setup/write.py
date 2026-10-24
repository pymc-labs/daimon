"""Agent creation and inline PAT helpers for Discord."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_lifecycle import create_user_agent
from daimon.core.constants import ALLOWED_MODEL_IDS
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import SpecError
from daimon.core.specs import (
    AgentSpec,
)
from pydantic import ValidationError

_log = structlog.get_logger()

if TYPE_CHECKING:
    pass


def validate_model_id(model: str) -> str | None:
    """Return an error message if `model` is not in the allow-list; None if valid.

    Discord modals cannot contain Select components, so we validate
    free-text input at submit time against ALLOWED_MODEL_IDS.
    """
    if model not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        return f"Model `{model}` is not allowed. Choose one of: {allowed}"
    return None


async def load_agent_inline_pat(runtime: DiscordRuntime, *, agent_id: uuid.UUID) -> str | None:
    return None


async def create_blank_agent(
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    name: str,
    system: str | None,
    model: str,
    account_id: uuid.UUID,
    creator_account_id: uuid.UUID | None = None,
) -> ResourceOutcome:
    """Build a blank AgentSpec from modal fields, create it, and start its face.

    Tenant-scoped name uniqueness: rejects if `name` already exists anywhere in
    this tenant, regardless of owner. Agent names are tenant-wide identity —
    reconcile dedup and the resolver key on (tenant, name) only. The person who
    pressed New agent (`creator_account_id`) owns it; `account_id` is the
    server account the agent is stamped with.
    """
    try:
        spec = AgentSpec.model_validate({"name": name, "model": model, "system": system})
    except ValidationError as err:
        raise SpecError(f"Spec validation failed: {err}") from err
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    outcome = await create_user_agent(
        runtime.anthropic,
        runtime.sessionmaker,
        tenant_id=tenant_id,
        spec=spec,
        account_id=account_id,
        public_url=public_url,
        creator_account_id=creator_account_id,
        taken_message=f"This server already has an agent named **{name}**. Pick a different name.",
    )
    if outcome.anthropic_id is not None:
        queue_agent_face(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            agent_name=name,
            metadata=None,  # user-owned: only the default name can make it built-in
            default_agent_name=runtime.deployment_default.agent_name,
        )
    return outcome


async def store_inline_pat(
    runtime: DiscordRuntime,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    plaintext_pat: str,
) -> str:
    raise ValueError("GitHub repository access requires a GitHub App grant.")

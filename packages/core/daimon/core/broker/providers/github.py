"""GitHub provider — reads the at-rest encrypted PAT via get_pat."""

from __future__ import annotations

import uuid
from typing import ClassVar

from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet, get_pat
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class GitHubTokenProvider:
    """Mints a token for the ``github`` service by reading the at-rest
    encrypted PAT bound to ``account_id`` (which today doubles as the
    principal id."""

    service: ClassVar[str] = "github"

    async def mint_token(
        self,
        *,
        account_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> str:
        if not settings.crypto.keys:
            raise ProviderConfigError(
                "github provider requires settings.crypto.keys to be configured"
            )
        fernet = build_multifernet(tuple(k.get_secret_value() for k in settings.crypto.keys))
        # NOTE: account_id IS principal_id today because
        # credentials are keyed on account_id-as-principal-id.
        # This may break if multi-principal accounts ship.
        # when agent_id is given, get_pat is overlay-only — if the agent has
        # no overlay row, None is returned and NoBindingError is raised here. This is
        # correct: an agent with no per-agent credential bound must not silently inherit
        # the principal-default PAT from another agent's Connect-GitHub action.
        token = await get_pat(
            principal_id=account_id,
            agent_id=agent_id,
            sessionmaker=sessionmaker,
            fernet=fernet,
        )
        if token is None:
            raise NoBindingError(
                "No GitHub credential bound to this account. Bind a PAT via the "
                "agent-setup repo-auth panel, or install the GitHub App on the repo."
            )
        return token

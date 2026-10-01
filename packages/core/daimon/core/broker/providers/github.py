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
        allow_service_default: bool = False,
    ) -> str:
        if not settings.crypto.keys:
            raise ProviderConfigError(
                "github provider requires settings.crypto.keys to be configured"
            )
        fernet = build_multifernet(tuple(k.get_secret_value() for k in settings.crypto.keys))
        # NOTE: account_id IS principal_id today because
        # credentials are keyed on account_id-as-principal-id.
        # This may break if multi-principal accounts ship.
        # when agent_id is given, get_pat is overlay-only — if the agent has no
        # overlay row and (when allow_service_default is set) no configured
        # operator fallback either, None is returned and NoBindingError is
        # raised here. This is correct: an agent with no per-agent credential
        # bound must not silently inherit the principal-default PAT from
        # another agent's Connect-GitHub action. Three-tier resolution:
        # overlay -> operator service default (opt-in, this caller only) -> raise.
        token = await get_pat(
            principal_id=account_id,
            agent_id=agent_id,
            sessionmaker=sessionmaker,
            fernet=fernet,
            allow_service_default=allow_service_default,
            fallback_pat=(
                settings.github.fallback_pat.get_secret_value()
                if settings.github.fallback_pat is not None
                else None
            ),
        )
        if token is None:
            if agent_id is None:
                raise NoBindingError(
                    "No GitHub token is bound to this caller's account. Ask the operator "
                    "to configure CLI token access for this account. Binding a target "
                    "agent's working repo does not supply an account-only call's token."
                )
            raise NoBindingError(
                "No GitHub access is bound for this agent. Use request_repo_binding "
                "to collect a token privately for the target agent. A GitHub App "
                "installation alone does not supply this CLI token."
            )
        return token

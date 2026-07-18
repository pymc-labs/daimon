"""TokenProvider Protocol."""

from __future__ import annotations

import uuid
from typing import ClassVar, Protocol, runtime_checkable

from daimon.core.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@runtime_checkable
class TokenProvider(Protocol):
    """A provider mints a short-lived access token for one external service."""

    service: ClassVar[str]

    async def mint_token(
        self,
        *,
        account_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> str: ...

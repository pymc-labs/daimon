"""What the data store spends and how it is billed: embeddings and reranking, metered through
the usual usage path and gated on the tenant's prepaid balance."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from daimon.core.config import Settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.pricing import side_call_rates
from daimon.core.tenant_balance import is_over_balance
from daimon.core.usage_recording import record_auxiliary_usage
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

type Purpose = Literal["data_embedding", "data_rerank"]

_PROVIDER: dict[Purpose, Provider] = {"data_embedding": "openai", "data_rerank": "gemini"}


class SessionMeter:
    """Bills one tenant's data spend as input units of a priced model (tokens for an
    embedding, 1,000 a request for the reranker)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        markup: Decimal = Decimal("1.0"),
        engine: AsyncEngine | None = None,
    ) -> None:
        self._sessions = sessionmaker
        self._markup = markup
        self._engine = engine

    @classmethod
    def of(cls, settings: Settings) -> SessionMeter:
        """A meter on a small pool of its own, for a process that has no engine (the sync
        worker). `close` it when the process ends."""
        engine = build_engine(str(settings.database.url), pool_size=2, max_overflow=2)
        return cls(
            build_session_factory(engine, crypto_keys=(), allow_plaintext=True),
            settings.billing.markup,
            engine,
        )

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    async def allowed(self, tenant_id: uuid.UUID) -> bool:
        """False for a prepaid tenant out of balance: its reads fall back to full text and its
        sources wait to be embedded."""
        return not await is_over_balance(sessionmaker=self._sessions, tenant_id=tenant_id)

    def priced(self, model: str) -> bool:
        try:
            side_call_rates(model)
        except KeyError:
            return False
        return True

    async def record(
        self,
        purpose: Purpose,
        *,
        tenant_id: uuid.UUID,
        platform_user_id: str | None,
        model: str,
        units: int,
    ) -> None:
        await record_auxiliary_usage(
            purpose=purpose,
            provider=_PROVIDER[purpose],
            sessionmaker=self._sessions,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            model_id=model,
            input_tokens=units,
            output_tokens=0,
            cache_read_input_tokens=0,
            markup=self._markup,
        )

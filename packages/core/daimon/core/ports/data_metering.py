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
from mux.contracts.ids import PROVIDERS, Provider
from mux.profiles import INLINE_REUSE
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

type Purpose = Literal["data_embedding", "data_rerank"]


class SessionMeter:
    """Bills one tenant's data spend as input units of a priced model (tokens for an
    embedding, 1,000 a query for the reranker). Usage rows name the embedding provider
    `DAIMON_DATA__EMBED_PROVIDER` names, when it is a provider family, else `selfhosted`."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        markup: Decimal = Decimal("1.0"),
        engine: AsyncEngine | None = None,
        embed_provider: str = "",
    ) -> None:
        self._sessions = sessionmaker
        self._markup = markup
        self._engine = engine
        name = embed_provider.strip()
        embedder: Provider = name if name in PROVIDERS else "selfhosted"
        # The reranker is Vertex AI's, the family INLINE_REUSE runs on.
        self._providers: dict[Purpose, Provider] = {
            "data_embedding": embedder,
            "data_rerank": INLINE_REUSE.provider,
        }

    @classmethod
    def of(cls, settings: Settings) -> SessionMeter:
        """A meter on a small pool of its own, for a process that has no engine (the sync
        worker). `close` it when the process ends."""
        engine = build_engine(str(settings.database.url), pool_size=2, max_overflow=2)
        sessions = build_session_factory(
            engine,
            crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys),
            allow_plaintext=settings.crypto.allow_plaintext,
        )
        return cls(sessions, settings.billing.markup, engine, settings.data.embed_provider)

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
            provider=self._providers[purpose],
            sessionmaker=self._sessions,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            model_id=model,
            input_tokens=units,
            output_tokens=0,
            cache_read_input_tokens=0,
            markup=self._markup,
        )

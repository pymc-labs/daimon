"""Compatibility constructor for explicit OpenAI native credential offers."""

from collections.abc import Callable
from datetime import UTC, datetime

from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.turn.native_credential_requests import (
    NativeCredentialAdapter,
)
from daimon.core.turn.native_credential_requests import (
    NativeCredentialDelivery as NativeCredentialDelivery,
)
from daimon.core.turn.native_credential_requests import (
    NativeCredentialOrigin as NativeCredentialOrigin,
)
from daimon.core.turn.native_credential_requests import (
    NativeCredentialRequest as NativeCredentialRequest,
)
from daimon.core.turn.native_credential_requests import (
    NativeOfferReceipt as NativeOfferReceipt,
)
from mux.profiles.openai import PERSISTENT_WORKSPACE
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROFILE = PERSISTENT_WORKSPACE.profile_id
CONTROL_PREFIX = "nc_openai_input:"


class OpenAINativeCredentialAdapter(NativeCredentialAdapter):
    """Select the provider profile; all authorization lives in the shared adapter."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        host: NeutralResourceHost,
        *,
        operator_enabled: bool = False,
        delivery: NativeCredentialDelivery | None = None,
        public_base_url: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(
            sessions,
            host,
            profile=PERSISTENT_WORKSPACE,
            operator_enabled=operator_enabled,
            delivery=delivery,
            public_base_url=public_base_url,
            now=now,
        )

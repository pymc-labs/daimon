"""Putting a credential from a private form straight onto a data source, the data package
answering (`daimon.data.ports`). The value never passes through core storage or chat."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from daimon.core import ports
from daimon.core.config import Settings

type SetCredential = Callable[[Settings, uuid.UUID, uuid.UUID, uuid.UUID, str], Awaitable[None]]
_REGISTERED: list[SetCredential] = []


class SourceCredentialRefused(Exception):
    """Nothing was saved; the message says why and never holds the value."""


def register_set_credential(write: SetCredential) -> None:
    _REGISTERED[:] = [write]


async def set_source_credential(
    settings: Settings, tenant_id: uuid.UUID, source_id: uuid.UUID, by: uuid.UUID, value: str
) -> None:
    """Seal `value` onto the tenant's source as `by`, or raise `SourceCredentialRefused`."""
    ports.load()
    if not _REGISTERED:
        raise SourceCredentialRefused("Data sources aren't set up here. Nothing was saved.")
    await _REGISTERED[0](settings, tenant_id, source_id, by, value)

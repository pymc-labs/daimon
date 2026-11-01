"""Notices for the web process, sent over Postgres NOTIFY.

A card raised by a tool in a web turn belongs in that person's browser, which only the
web process reaches. A notice names the row it is about and carries no secret: the
credential request's token never leaves its row.
"""

from __future__ import annotations

import uuid
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

WEB_NOTICE_CHANNEL: Final = "daimon_web"


class WebCredentialNotice(BaseModel):
    """A credential request was posted to a web chat thread."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["credential"] = "credential"
    # The web chat's thread key, "web:<thread id>".
    thread_id: str
    # The request's idempotency key: its card id, never its token.
    request_id: uuid.UUID


async def send_web_notice(session: AsyncSession, notice: WebCredentialNotice) -> None:
    """Queue the notice; Postgres delivers it when the transaction commits."""
    await session.execute(select(func.pg_notify(WEB_NOTICE_CHANNEL, notice.model_dump_json())))


def parse_web_notice(payload: str) -> WebCredentialNotice | None:
    """The notice in a payload, or None for one this version does not know."""
    try:
        return WebCredentialNotice.model_validate_json(payload)
    except ValidationError:
        return None

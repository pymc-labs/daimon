"""Notices for the web process, sent over Postgres NOTIFY.

A card raised by a tool in a web turn belongs in that person's browser, which only the
web process reaches. A notice names the row it is about and carries no secret: the
credential request's token never leaves its row.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
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


class WebControlNotice(BaseModel):
    """A card Daimon answers itself (a host call, a switch) was posted to a web chat thread."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["control"] = "control"
    thread_id: str
    # "hcc:<id>" or "bsw:<id>": the press route's card id and the record's message id.
    card_id: str
    title: str
    details: tuple[str, ...] = ()
    footer: str | None = None


WebNotice = Annotated[WebCredentialNotice | WebControlNotice, Field(discriminator="kind")]
_NOTICE: TypeAdapter[WebNotice] = TypeAdapter(WebNotice)


async def send_web_notice(session: AsyncSession, notice: WebNotice) -> None:
    """Queue the notice; Postgres delivers it when the transaction commits."""
    await session.execute(select(func.pg_notify(WEB_NOTICE_CHANNEL, notice.model_dump_json())))


def parse_web_notice(payload: str) -> WebNotice | None:
    """The notice in a payload, or None for one this version does not know."""
    try:
        return _NOTICE.validate_json(payload)
    except ValidationError:
        return None

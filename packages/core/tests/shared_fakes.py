"""Fakes and helpers that several test files in this directory share."""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from typing import Self

import httpx
import pytest
from daimon.core._models import TaskContinuation
from daimon.core.stores.domain import Role, TaskContinuationRow, TurnOriginRow
from daimon.core.turn_origin import turn_origin
from daimon.core.wizard.state import WizardState, WizardStatus
from mux.contracts.events import Event, NativeProvenance
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine


class SessionFactory:
    def __call__(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)

    def begin(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)


def assert_skill_multipart(request: httpx.Request) -> bytes:
    """Pin the old SDK file tuple at the actual host upload call site."""
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    files = [part for part in message.iter_parts() if part.get_filename() is not None]
    assert len(files) == 1
    part = files[0]
    assert part.get_param("name", header="content-disposition") == "files[]"
    assert part.get_filename() == "SKILL.zip"
    assert part.get_content_type() == "application/zip"
    data = part.get_payload(decode=True)
    assert isinstance(data, bytes)
    return data


def concurrency_dsn() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for the concurrency test")
    return url


def control_card(kind: str, requester: str):
    """A posted control card with Cancel and the filled action rightmost, as posted cards are."""
    import datetime as dt
    import uuid

    from daimon.core.posted_controls import FOOTER_TEMPLATE, CardButton, PostedCard

    card_id = uuid.uuid4()
    return PostedCard(
        kind=kind,  # pyright: ignore[reportArgumentType]
        state="requested",
        headline="Change the model?",
        facts=("Researcher moves to OpenAI.",),
        buttons=(
            CardButton(label="Cancel", style="secondary", custom_id=f"bsw:{card_id}:cancel"),
            CardButton(label="Switch", style="primary", custom_id=f"bsw:{card_id}:confirm"),
        ),
        footer=FOOTER_TEMPLATE,
        requester_platform_user_id=requester,
        expires_at_unix=int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
    )


def _state(
    *,
    current_step: int = 0,
    answers: dict[str, list[str]] | None = None,
    status: WizardStatus = WizardStatus.OPEN,
) -> WizardState:
    return WizardState(
        short_id="abcd1234",
        current_step=current_step,
        answers=answers if answers is not None else {},
        status=status,
    )


async def until_lock_wait(engine: AsyncEngine, pid: int) -> None:
    async with engine.connect() as probe:
        for _ in range(200):
            waiting = (
                await probe.execute(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
            ).scalar_one_or_none()
            if waiting == "Lock":
                return
            await probe.rollback()
            await asyncio.sleep(0.025)
    raise AssertionError(f"backend {pid} never waited on a lock")


async def pending_wakes(db, *, tenant_id, platform, thread_id):
    """Pending wakes for one thread, including any due after the database clock.

    These tests run a fake clock ahead of the database, so a wake written at
    that clock is not yet due for `list_pending_continuations`.
    """
    rows = (
        await db.execute(
            select(TaskContinuation)
            .where(
                TaskContinuation.tenant_id == tenant_id,
                TaskContinuation.platform == platform,
                TaskContinuation.thread_id == thread_id,
                TaskContinuation.status == "pending",
            )
            .order_by(TaskContinuation.created_at, TaskContinuation.id)
        )
    ).scalars()
    return [TaskContinuationRow.model_validate(row) for row in rows]


def wake_origin(
    sessionmaker, row: TaskContinuationRow, account_id, **fields
) -> contextlib.AbstractAsyncContextManager[TurnOriginRow]:
    """The origin an adapter opens for the turn that wake `row` starts."""
    return turn_origin(
        sessionmaker,
        tenant_id=row.tenant_id,
        account_id=account_id,
        platform=row.platform,
        parent_channel_id=row.parent_channel_id,
        thread_id=row.thread_id,
        responder_ma_agent_id=row.target_ma_agent_id,
        responder_name=row.target_name,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        attended=False,
        **fields,
    )


def terminal(provider: str, outcome: str) -> Event:
    return Event.model_validate(
        {
            "id": "ended:" + outcome,
            "session_id": "session",
            "sequence": 0,
            "type": "session.turn_ended",
            "authority": "record",
            "observed_at": datetime.now(UTC),
            "turn_id": "root",
            "payload": {"root_turn_id": "root", "outcome": outcome},
            "native": NativeProvenance(provider=provider, api_revision="offline"),
        }
    )

"""Drive one chat turn through the core pipeline and read back what it left behind."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from daimon.core.turn.prepare import PreparedTurn, bind_session
from daimon.core.turn.run import RunOutcome
from daimon.testing.builders import admit_chat
from daimon.testing.recovery import Process
from daimon.testing.turn_fakes import RecordingLifecycle, run_prepared

PLATFORM = "slack"
USER = "writer"
CHANNEL = "channel"
THREAD = "thread"


async def _no_reseed() -> str:
    return "history reseed"


@dataclass
class ChatTurn:
    """A chat turn in flight; `prepared` is set once the session is bound."""

    process: Process
    tenant_id: uuid.UUID
    text: str
    lifecycle: RecordingLifecycle
    thread_id: str = THREAD
    prepared: PreparedTurn | None = None
    outcome: RunOutcome | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self) -> RunOutcome:
        deps = self.process.deps
        admission = await admit_chat(
            deps,
            self.tenant_id,
            platform=PLATFORM,
            external_user_id=USER,
            channel_id=CHANNEL,
            thread_id=self.thread_id,
            now=datetime.now(UTC),
        )
        self.prepared = await bind_session(
            deps,
            admission,
            tenant_id=self.tenant_id,
            platform=PLATFORM,
            external_user_id=USER,
            thread_id=self.thread_id,
            session_account_id=admission.account_id,
            reuse_existing=True,
        )
        self.outcome = await run_prepared(
            deps,
            self.prepared,
            tenant_id=self.tenant_id,
            platform=PLATFORM,
            thread_id=self.thread_id,
            external_user_id=USER,
            user_message=self.text,
            lifecycle=self.lifecycle,
            cancel=self.cancel,
            reseed_user_message=_no_reseed,
        )
        return self.outcome


def start_chat(
    process: Process, tenant_id: uuid.UUID, text: str, lifecycle: RecordingLifecycle | None = None
) -> tuple[ChatTurn, asyncio.Task[RunOutcome]]:
    turn = ChatTurn(process, tenant_id, text, lifecycle or RecordingLifecycle())
    return turn, process.spawn(turn.run())

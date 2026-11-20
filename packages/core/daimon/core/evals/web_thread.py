"""`ThreadSource` for web chats: a person's own threads, read from `web_messages`.

The web chat keeps the agent's steps as labels, not tool arguments or results, so a web thread
is a text recording; its files are names only.
"""

from __future__ import annotations

import uuid

from daimon.core.evals.from_thread import (
    ThreadFile,
    ThreadMessage,
    ThreadNotFound,
    ThreadPerson,
    ThreadRecord,
)
from daimon.core.stores import web_chat, web_people
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_BOT = "agent"


def thread_id_of(ref: str) -> uuid.UUID:
    """The chat id in `ref`: a bare id, or the last part of a link to the chat."""
    tail = ref.strip().rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    try:
        return uuid.UUID(tail)
    except ValueError:
        raise ThreadNotFound(ref) from None


class WebChatSource:
    """Reads threads of one web person; someone else's thread reads as not found.

    Without `person_id` (an operator) it reads any live thread of the workspace.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        tenant_id: uuid.UUID,
        person_id: uuid.UUID | None = None,
    ) -> None:
        self._sessions = sessions
        self._tenant_id = tenant_id
        self._person_id = person_id

    async def thread(self, ref: str) -> ThreadRecord:
        thread_id = thread_id_of(ref)
        async with self._sessions() as session:
            if self._person_id is None:
                thread = await web_chat.get_tenant_thread(
                    session, tenant_id=self._tenant_id, thread_id=thread_id
                )
            else:
                thread = await web_chat.get_thread(
                    session, person_id=self._person_id, thread_id=thread_id
                )
            person = thread and await web_people.get_person(
                session, tenant_id=self._tenant_id, person_id=thread.person_id
            )
            rows = await web_chat.list_messages(session, thread_id=thread_id)
        if thread is None or person is None or thread.tenant_id != self._tenant_id:
            raise ThreadNotFound(ref)
        me = str(person.id)
        return ThreadRecord(
            platform="web",
            ref=str(thread.id),
            conversation="dm",
            title=thread.title,
            people=[
                ThreadPerson(id=me, name=person.display_name, email=person.email),
                ThreadPerson(id=_BOT, name=thread.agent_name, is_bot=True),
            ],
            messages=[
                ThreadMessage(
                    id=str(row.id),
                    author=me if row.role == "user" else _BOT,
                    at=row.created_at,
                    text=row.content,
                    files=[ThreadFile(name=a.name) for a in row.blocks.attachments],
                )
                for row in rows
                if row.status != "running"
            ],
        )

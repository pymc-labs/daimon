"""Teams thread keys, shared by the Teams adapter and the MCP server.

A Teams 1:1 chat has no threads, so a setup conversation in one is keyed
`<chat>;setup=<id>`. Anything that posts for such a key posts to the chat.
"""

from __future__ import annotations

import uuid

_SETUP = ";setup="


def new_setup_thread_id(chat: str) -> str:
    """A fresh setup-conversation key under a 1:1 chat."""
    return f"{chat}{_SETUP}{uuid.uuid4().hex}"


def conversation_of(thread_id: str) -> str:
    """The Bot Framework conversation a thread key posts to."""
    return thread_id.split(_SETUP, 1)[0]


def is_group_chat(conversation_id: str) -> bool:
    """A Teams group (or meeting) chat id, `19:...@thread.v2`, with or without a suffix."""
    head = conversation_id.split(";", 1)[0]
    return head.startswith("19:") and head.endswith("@thread.v2")

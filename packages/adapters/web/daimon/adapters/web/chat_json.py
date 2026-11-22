"""The chat's JSON shapes, as the web app reads them."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.monitoring import MonitoringNotice
from daimon.core.stores.domain import WebMessageRow, WebThreadRow
from pydantic import BaseModel

#: What an agent that is gone is called wherever it is still named.
UNKNOWN_AGENT = "Removed agent"


class AgentRef(BaseModel):
    id: str
    name: str
    avatar: str
    model: str
    description: str


def agent_ref(agent: BetaManagedAgentsAgent | None, agent_id: str, avatar: str = "") -> AgentRef:
    """An agent as the app shows it; one that is gone keeps its id only."""
    if agent is None:
        return AgentRef(id=agent_id, name=UNKNOWN_AGENT, avatar="", model="", description="")
    return AgentRef(
        id=agent.id,
        name=agent.name,
        avatar=avatar,
        model=agent.model.id,
        description=agent.description or "",
    )


def agent_json(
    agent: BetaManagedAgentsAgent | None,
    thread: WebThreadRow | None = None,
    avatar: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """An agent card; a thread whose agent is gone keeps the name it was started with.

    `model` is the one a chat answers on when that is not the agent's own record.
    """
    if agent is None:
        assert thread is not None
        return {
            "id": thread.agent_id,
            "name": thread.agent_name,
            "avatar": "",
            "model": "",
            "description": "",
        }
    return {
        "id": agent.id,
        "name": agent.name,
        "avatar": avatar,
        "model": model if model is not None else agent.model.id,
        "description": agent.description or "",
    }


def session_json(
    thread: WebThreadRow,
    agent: BetaManagedAgentsAgent | None,
    *,
    running: bool = False,
    avatar: str = "",
    model: str | None = None,
    pages: Mapping[uuid.UUID, str] | None = None,
) -> dict[str, Any]:
    """`pages` names the artifact of an ask thread (a thread a page's Ask box started)."""
    page = thread.artifact_id
    title = None if page is None or pages is None else pages.get(page)
    return {
        "id": str(thread.id),
        "title": thread.title,
        "agent": agent_json(agent, thread, avatar, model),
        "updated_at": thread.updated_at.isoformat(),
        "running": running,
        "artifact": None if title is None else {"id": str(page), "title": title},
    }


def notice_json(notice: MonitoringNotice) -> dict[str, str]:
    return {"line": notice.line, "help": notice.help}


def message_json(row: WebMessageRow) -> dict[str, Any]:
    blocks = row.blocks
    msg: dict[str, Any] = {
        "id": str(row.id),
        "role": row.role,
        "content": row.content,
        "created_at": row.created_at.isoformat(),
    }
    if row.role == "assistant":
        msg["steps"] = [step.model_dump(exclude_none=True) for step in blocks.steps]
        msg["feedback"] = row.feedback
        if row.status in ("stopped", "interrupted"):
            msg["stopped"] = True
        if row.status == "running":
            msg["streaming"] = True
        if row.duration_ms is not None:
            msg["ms"] = row.duration_ms
        if row.cost_usd is not None:
            msg["cost_usd"] = float(row.cost_usd)
    if blocks.files:
        msg["files"] = [f.model_dump(mode="json") for f in blocks.files]
    if blocks.attachments:
        msg["attachments"] = [{"name": a.name, "size": a.size} for a in blocks.attachments]
    if blocks.artifacts:
        msg["artifacts"] = [a.model_dump(mode="json") for a in blocks.artifacts]
    if blocks.card is not None:
        msg["card"] = blocks.card.model_dump(mode="json", exclude_none=True)
    return msg

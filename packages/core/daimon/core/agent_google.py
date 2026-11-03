"""The Google Workspace identity an operator bound to an agent, read-only."""

from __future__ import annotations

from daimon.core.agent_editing import AgentActor, AgentEditDeps
from daimon.core.agent_target import load_target, require_setup
from daimon.core.stores.agent_google_binding import get_agent_google_binding
from pydantic import BaseModel, ConfigDict


class GoogleBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    scopes: list[str]


async def get_binding(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str
) -> GoogleBinding | None:
    """The agent's Google identity and its scopes, or None when it has none."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    require_setup(target, "Google access")
    async with deps.session_factory() as session:
        row = await get_agent_google_binding(session, agent_id=target.agent_id)
    return None if row is None else GoogleBinding(email=row.email, scopes=list(row.scopes))

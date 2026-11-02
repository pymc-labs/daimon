"""Who may chat with an agent on the web. Pure; the stores feed it."""

from __future__ import annotations

from collections.abc import Set

from daimon.core.stores.domain import WebAgentGrantRow


def may_chat(
    agent_id: str,
    grant: WebAgentGrantRow | None,
    *,
    is_visitor: bool,
    is_admin: bool,
    groups: Set[str],
    invited_agent_ids: Set[str],
) -> bool:
    """A visitor reaches only their invite's agents; an admin every agent that is on.

    No grant means the agent is on for everyone.
    """
    if grant is not None and not grant.web:
        return False
    if is_visitor:
        return agent_id in invited_agent_ids
    if is_admin or grant is None or grant.who == "everyone":
        return True
    if grant.who == "groups":
        return not groups.isdisjoint(grant.groups)
    return agent_id in invited_agent_ids

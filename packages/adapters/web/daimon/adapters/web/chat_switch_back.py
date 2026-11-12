"""The "Switch back to Claude" button on a refusal card."""

from __future__ import annotations

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.agent_access import actor_of, change_deps
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.model_switch import SwitchRefusal, back_to_claude
from daimon.core.stores.domain import WebThreadRow
from daimon.core.web_identity import place_of
from fastapi import HTTPException

_STATUS = {"owner_or_admin_required": 403, "forbidden": 403}


async def switch_back(runtime: WebRuntime, caller: WebCaller, thread: WebThreadRow) -> str:
    """Move the chat's agent back to Claude, or clear the pin that holds it. The line to show.

    Refused (403, 409) with the words to show when the person may not, or it can't be done.
    """
    deps = change_deps(runtime, caller)
    if deps.backends is None:
        raise HTTPException(status_code=409, detail="Choosing a model provider is turned off here.")
    done = await back_to_claude(
        deps.backends,
        deps.edit,
        actor=actor_of(caller),
        agent_name=thread.agent_name,
        platform="web",
        channel_id=place_of(caller.person.id),
        admin=is_admin(caller.role),
    )
    if isinstance(done, SwitchRefusal):
        raise HTTPException(status_code=_STATUS.get(done.reason, 409), detail=done.line)
    return done

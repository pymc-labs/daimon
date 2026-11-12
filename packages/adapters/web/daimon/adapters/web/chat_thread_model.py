"""One chat's model: the choices, what a pick changes, and keeping it.

The chat-view control of the same `daimon.core.thread_model_switch` Discord `/model`
uses: a pick on the agent's own provider is kept at once, any other is previewed and
confirmed once. The person must own the agent or be an admin to move it off its provider,
unless its `thread_providers` feature is on.
"""

from __future__ import annotations

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.agent_access import actor_of, change_deps
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.model_preview import ModelPreview, preview_doc, refused_doc
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.channel_backend import BackendActor, load_backend_agent
from daimon.core.model_switch import SwitchRefusal
from daimon.core.stores.domain import WebThreadRow
from daimon.core.stores.thread_models import get_thread_choice
from daimon.core.thread_model_switch import (
    ThreadPick,
    confirm_thread_or_refusal,
    pick_value,
    preview_thread_or_refusal,
    thread_model_picker,
)
from daimon.core.thread_models import choose_thread_model
from daimon.core.web_identity import thread_key
from fastapi import HTTPException
from pydantic import BaseModel

_STATUS = {"owner_or_admin_required": 403, "forbidden": 403}
#: What the preview says its scope is: a chat's pick changes nothing else.
THIS_CHAT = "This chat only."


class ModelOption(BaseModel):
    value: str
    label: str
    price: str
    provider: str


class ThreadModels(BaseModel):
    """What this chat can pick from; `current` is the pick it runs on, None for the agent's own."""

    current: str | None
    choices: list[ModelOption]


class Picked(BaseModel):
    model: str | None
    note: str


async def thread_pick(runtime: WebRuntime, caller: WebCaller, thread: WebThreadRow) -> ThreadPick:
    """The pick for this chat's agent, as this person."""
    deps = change_deps(runtime, caller)
    if deps.backends is None:
        raise HTTPException(status_code=409, detail="Choosing a model is turned off here.")
    tenant_id = caller.person.tenant_id
    agent = await load_backend_agent(
        deps.edit.client, runtime.sessionmaker, tenant_id=tenant_id, agent_name=thread.agent_name
    )
    actor = actor_of(caller)
    return ThreadPick(
        service=deps.backends,
        sessionmaker=runtime.sessionmaker,
        settings=runtime.settings,
        agent=agent,
        actor=BackendActor(tenant_id=tenant_id, account_id=actor.account_id, subject=actor.subject),
        ma_agent_id=thread.agent_id,
        platform="web",
        platform_user_id=str(caller.person.id),
    )


async def thread_models(
    runtime: WebRuntime, caller: WebCaller, thread: WebThreadRow
) -> ThreadModels:
    """Every model this chat can run on, priced, and the one it has picked."""
    pick = await thread_pick(runtime, caller, thread)
    picker = await thread_model_picker(pick)
    async with runtime.sessionmaker() as db:
        chosen = await get_thread_choice(
            db,
            tenant_id=caller.person.tenant_id,
            platform="web",
            thread_id=thread_key(thread.id),
        )
    return ThreadModels(
        current=f"{chosen.provider}:{chosen.id}" if chosen is not None else None,
        choices=[
            ModelOption(value=c.value, label=c.label, price=c.price or "", provider=c.provider)
            for c in picker.choices
        ],
    )


async def preview_thread(
    runtime: WebRuntime, caller: WebCaller, thread: WebThreadRow, value: str
) -> ModelPreview:
    """What picking `value` changes for this chat, or why it can't be picked."""
    pick = await thread_pick(runtime, caller, thread)
    found = await preview_thread_or_refusal(pick, pick_value(value), admin=is_admin(caller.role))
    if isinstance(found, SwitchRefusal):
        return refused_doc(found)
    doc = preview_doc(found, agent_name=thread.agent_name)
    return doc.model_copy(update={"threads": THIS_CHAT if doc.moves else ""})


async def keep_thread_model(
    runtime: WebRuntime,
    caller: WebCaller,
    thread: WebThreadRow,
    *,
    value: str,
    digest: str,
    revision: int,
) -> Picked:
    """Keep the pick the person confirmed; `default` goes back to the agent's own model."""
    if value.strip().lower() == "default":
        change = await choose_thread_model(
            runtime.sessionmaker,
            tenant_id=caller.person.tenant_id,
            platform="web",
            thread_id=thread_key(thread.id),
            choice="default",
        )
        return Picked(model=change.model_id, note=change.note)
    pick = await thread_pick(runtime, caller, thread)
    done = await confirm_thread_or_refusal(
        pick,
        pick_value(value),
        thread_id=thread_key(thread.id),
        digest=digest,
        revision=revision,
        admin=is_admin(caller.role),
    )
    if isinstance(done, SwitchRefusal):
        raise HTTPException(status_code=_STATUS.get(done.reason, 409), detail=done.line)
    return Picked(model=done.model_id, note=done.note)

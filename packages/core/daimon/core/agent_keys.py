"""An agent's stored keys (its `.env`): the names, and adding or removing one.

Values go in and never come out. The rules are the chat key flow's: a new name
is a contribution any setup-seeing actor may make; replacing or removing takes
an admin or the agent's owner (`operation_policy`).
"""

from __future__ import annotations

from datetime import datetime

from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import AgentTarget, ChangeRecorder, load_target, require_setup
from daimon.core.constants import MAX_SECRET_VALUE_BYTES
from daimon.core.credential_submit import env_name_refusal
from daimon.core.env_file import env_alias_shadowed, env_name_problem
from daimon.core.errors import StoreError
from daimon.core.operation_policy import OperationKind, TargetFacts, decide_operation
from daimon.core.stores.agent_files import (
    AgentEnvEncryptionRequiredError,
    agent_env_writes_allowed,
    delete_agent_file,
    list_agent_file_revisions,
    lock_agent_keys,
    put_agent_file_if_unchanged,
)
from pydantic import BaseModel, ConfigDict


class KeyName(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    updated_at: datetime


def _check_allowed(
    target: AgentTarget, actor: AgentActor, operation: OperationKind, key: str
) -> None:
    outcome = decide_operation(
        operation,
        is_admin=actor.subject.is_admin,
        target=TargetFacts(
            is_daimon_managed=target.managed,
            is_reachable_in_tenant=False,
            owned_by_caller=target.owned,
        ),
    )
    if outcome != "allow":
        verb = "Removing" if operation == "key_remove" else "Replacing"
        raise AgentEditError(f"{verb} {key} needs the agent's owner or an admin.", kind="forbidden")


async def list_key_names(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str
) -> list[KeyName]:
    """The names of the agent's stored keys, for an admin or the agent's owner."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    require_setup(target, "Keys")
    async with deps.session_factory() as session:
        rows = await list_agent_file_revisions(
            session, tenant_id=actor.tenant_id, agent_id=target.agent_id
        )
    return [KeyName(key=key, updated_at=at) for key, at in rows]


async def set_key(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    key: str,
    value: str,
    audited: ChangeRecorder | None = None,
) -> KeyName:
    """Store ``value`` under ``key``: a new name, or a replacement where the actor may."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    require_setup(target, "Keys")
    if not value.strip():
        raise AgentEditError("Enter the value first.", kind="invalid")
    if len(value.encode()) > MAX_SECRET_VALUE_BYTES:
        raise AgentEditError(
            f"That value is too large. Max {MAX_SECRET_VALUE_BYTES} bytes.", kind="invalid"
        )
    problem = env_name_problem(key, is_admin=actor.subject.is_admin)
    if problem is not None:
        raise AgentEditError(env_name_refusal(key, problem), kind="invalid")
    async with deps.session_factory() as session:
        if not agent_env_writes_allowed(session):
            raise AgentEditError(str(AgentEnvEncryptionRequiredError()), kind="invalid")
    async with deps.session_factory() as session, session.begin():
        await lock_agent_keys(session, tenant_id=actor.tenant_id, agent_id=target.agent_id)
        held = dict(
            await list_agent_file_revisions(
                session, tenant_id=actor.tenant_id, agent_id=target.agent_id
            )
        )
        replaces = held.get(key)
        if replaces is not None or env_alias_shadowed(key, held) is not None:
            _check_allowed(target, actor, "key_replace", key)
        else:
            _check_allowed(target, actor, "key_add", key)
        try:
            written = await put_agent_file_if_unchanged(
                session,
                tenant_id=actor.tenant_id,
                agent_id=target.agent_id,
                key=key,
                content=value,
                set_by_account_id=actor.account_id,
                expected_updated_at=replaces,
            )
        except StoreError as exc:
            raise AgentEditError(str(exc), kind="invalid") from exc
        if written is not None and audited is not None:
            await audited(session, target.agent.name)
    if written is None:
        raise AgentEditError(f"{key} changed meanwhile. Reload and try again.", kind="stale")
    return KeyName(key=written.key, updated_at=written.updated_at)


async def remove_key(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    key: str,
    audited: ChangeRecorder | None = None,
) -> bool:
    """Remove ``key``; whether it was there."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    require_setup(target, "Keys")
    _check_allowed(target, actor, "key_remove", key)
    async with deps.session_factory() as session, session.begin():
        await lock_agent_keys(session, tenant_id=actor.tenant_id, agent_id=target.agent_id)
        held = await list_agent_file_revisions(
            session, tenant_id=actor.tenant_id, agent_id=target.agent_id
        )
        await delete_agent_file(
            session, tenant_id=actor.tenant_id, agent_id=target.agent_id, key=key
        )
        if audited is not None and any(name == key for name, _ in held):
            await audited(session, target.agent.name)
    return any(name == key for name, _ in held)

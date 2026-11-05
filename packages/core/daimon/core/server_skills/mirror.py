"""Add a skill a connected server serves to the library, as a mirror the sync keeps current."""

from __future__ import annotations

from datetime import datetime

from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import ChangeRecorder
from daimon.core.mcp_skills.install import ServerSkill
from daimon.core.server_skills.anthropic import PROVIDER
from daimon.core.server_skills.gate import allowed_tools_of, manifest_digest, manifest_of
from daimon.core.server_skills.sync import version_label
from daimon.core.skill_library_files import SkillRef, create_custom_skill, skill_lock
from daimon.core.stores import server_skills as store

__all__ = ["add_server_skill"]


async def add_server_skill(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    server: str,
    fetched: ServerSkill,
    now: datetime,
    audited: ChangeRecorder | None = None,
) -> SkillRef:
    """The library skill mirroring `fetched`, created on first add with every ingest check.

    Any editor may add one: the server decides who sees which skill, and the mirror is
    read-only. Adding one already mirrored returns it.
    """
    entry = fetched.entry
    async with skill_lock(deps, tenant_id=actor.tenant_id, skill_id=f"{server}\n{entry.uri}"):
        async with deps.session_factory() as session:
            held = await store.find_mirror(
                session, tenant_id=actor.tenant_id, server=server, uri=entry.uri
            )
            deployed = None if held is None else await store.list_deployments(session, held.id)
        if held is not None and deployed is not None and PROVIDER in deployed:
            return SkillRef(id=held.skill_id, name=held.name, version=deployed[PROVIDER].version)
        if held is not None:
            raise AgentEditError("This skill is still being added. Try again.", kind="conflict")
        ref = await create_custom_skill(deps, actor=actor, files=fetched.files)
        if ref.name != entry.name:
            raise AgentEditError(f"The skill's SKILL.md must name {entry.name}.", kind="invalid")
        manifest = manifest_of(entry)
        async with deps.session_factory() as session, session.begin():
            mirror = await store.create_mirror(
                session,
                tenant_id=actor.tenant_id,
                server=server,
                uri=entry.uri,
                name=ref.name,
                skill_id=ref.id,
                version_label=version_label(entry),
                digest=manifest_digest(manifest),
                manifest=manifest,
                allowed_tools=allowed_tools_of(entry.frontmatter),
                added_by_account_id=actor.account_id,
                now=now,
            )
            await store.put_deployment(
                session,
                mirror_id=mirror.id,
                tenant_id=actor.tenant_id,
                provider=PROVIDER,
                digest=mirror.digest,
                version=ref.version,
                now=now,
            )
            if audited is not None:
                await audited(session, f"{ref.name} v{ref.version}")
    return ref

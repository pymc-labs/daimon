"""`/api/agents/{id}/github`: an agent's repos, skill sources and sync, and its owner's actions."""

from __future__ import annotations

from daimon.adapters.web.agent_access import Editor, Viewer, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_github
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_github import GitHubPanel, ResyncResults, SkillResyncDeps
from daimon.core.github_panel import connect_root
from fastapi import APIRouter
from pydantic import BaseModel


class ConnectLink(BaseModel):
    url: str


class WorkingBody(BaseModel):
    repo: str | None


def agent_github_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    def root() -> str | None:
        return connect_root(runtime.settings)

    @router.get("/{agent_id}/github")
    async def panel(caller: Viewer, agent_id: str) -> GitHubPanel:  # pyright: ignore[reportUnusedFunction]
        try:
            return await agent_github.get_panel(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                configured=root() is not None,
                version_times=runtime.github_version_times,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/{agent_id}/github/connect")
    async def connect(caller: Editor, agent_id: str) -> ConnectLink:  # pyright: ignore[reportUnusedFunction]
        try:
            url = await agent_github.connect_url(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                root=root(),
                fernet=runtime.fernet,
                audited=recorded(runtime, caller, "Started GitHub connect"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return ConnectLink(url=url)

    @router.put("/{agent_id}/github/working")
    async def working(caller: Editor, agent_id: str, body: WorkingBody) -> GitHubPanel:  # pyright: ignore[reportUnusedFunction]
        try:
            return await agent_github.set_working(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                repo=body.repo,
                configured=root() is not None,
                version_times=runtime.github_version_times,
                audited=recorded(runtime, caller, "Set working repo", body.repo),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.delete("/{agent_id}/github/repos/{owner}/{name}")
    async def remove(caller: Editor, agent_id: str, owner: str, name: str) -> GitHubPanel:  # pyright: ignore[reportUnusedFunction]
        repo = f"{owner}/{name}"
        try:
            return await agent_github.remove_repo(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                repo=repo,
                configured=root() is not None,
                version_times=runtime.github_version_times,
                audited=recorded(runtime, caller, "Removed GitHub repo", repo),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/{agent_id}/github/resync")
    async def resync(caller: Editor, agent_id: str) -> ResyncResults:  # pyright: ignore[reportUnusedFunction]
        settings = runtime.settings
        try:
            return await agent_github.resync(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                sync=SkillResyncDeps(
                    fernet=runtime.fernet,
                    http_client=runtime.http_client,
                    app_id=settings.github_app.app_id,
                    app_private_key=settings.github_app.private_key,
                    max_tarball_bytes=settings.github.max_tarball_bytes,
                    max_tarball_decompressed_bytes=settings.github.max_tarball_decompressed_bytes,
                ),
                audited=recorded(runtime, caller, "Resynced repo skills"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    return router

"""add_skill: add one pasted, attached or GitHub skill to one agent, after a preview.

``register_skill_upload_tools(mcp, runtime)`` wires the tool; ``_add_skill_impl``
is testable without a FastMCP Context. The checks and the upload live in
``daimon.core.skills``; this module picks the source, gates the caller and
turns refusals into ``ToolError``.

A call without ``content_hash`` only previews. The upload needs the hash that
preview returned, so the person sees what is added before it is, and with tool
safety on that second call also waits for their Approve on the card.
"""

from __future__ import annotations

import time
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import unquote, urlparse

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import InvalidToken
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import reachability
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.agents import (
    _system_agent_rejection,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import resolve_setup_agent
from daimon.adapters.mcp.tools.skills import (
    _resolve_sync_token,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import decrypt_token
from daimon.core.operation_policy import OperationKind, decide_operation
from daimon.core.skill_zip import MAX_UNCOMPRESSED_BYTES
from daimon.core.skills.add import (
    SkillAddResult,
    add_agent_skill,
    fetch_attachment,
    fetch_repo_skill,
)
from daimon.core.skills.fetch import GitHubFetchError
from daimon.core.skills.ingest import (
    SkillBundle,
    SkillPreview,
    bundle_from_markdown,
    bundle_from_upload,
)
from daimon.core.slack_file_token import verify_file_token
from daimon.core.slack_files import fetch_slack_file
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AddSkillResult", "register_skill_upload_tools", "require_skill_change"]

#: The only hosts Discord serves attachments from (as `discord_send`'s allowlist).
_DISCORD_ATTACHMENT_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
_SLACK_FILE_PATH = "/slack/file/"


class AddSkillResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["preview", "added"]
    agent_name: str
    preview: SkillPreview
    added: SkillAddResult | None = None
    summary: str


async def require_skill_change(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent: BetaManagedAgentsAgent,
    *,
    agent_name: str,
    operation: OperationKind,
) -> None:
    """Raise unless the caller may add or remove one of `agent`'s skills.

    A built-in agent is forked first; a server admin may change any other; a
    channel admin one that answers only in their channels; anyone one that
    answers nowhere.
    """
    rejection = _system_agent_rejection(agent)
    if rejection is not None:
        raise ToolError(rejection)
    facts = await reachability.target_facts(
        runtime,
        auth,
        operation,
        agent_name=agent_name,
        is_daimon_managed=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
    )
    if decide_operation(operation, is_admin=auth.is_admin, target=facts) != "allow":
        raise ToolError(
            f"'{agent_name}' answers in a channel or the whole workspace, so changing its "
            "skills needs a workspace or server admin, or an admin of every channel it "
            "answers in. Tell the caller an admin can ask Daimon to make this change. "
            "Nothing was changed. Do not retry."
        )


async def _load_bundle(
    runtime: McpRuntime,
    auth: AuthIdentity,
    http: httpx.AsyncClient,
    *,
    skill_md: str | None,
    attachment_url: str | None,
    repo_url: str | None,
    branch: str,
    path: str,
) -> tuple[SkillBundle, str]:
    """The checked skill and a short origin for the ledger."""
    if skill_md is not None:
        return bundle_from_markdown(skill_md), "pasted"
    if attachment_url is not None:
        data, filename = await _fetch_platform_attachment(runtime, auth, http, attachment_url)
        return bundle_from_upload(data, filename=filename), f"attachment {filename}"
    if repo_url is None:
        raise ToolError("Pass exactly one of skill_md, attachment_url or repo_url.")
    # The workspace's stored GitHub access fetches only for an admin, so a
    # member cannot lift files out of a private repo the workspace enrolled.
    token = await _resolve_sync_token(runtime, auth, repo_url, http) if auth.is_admin else None
    try:
        bundle = await fetch_repo_skill(
            http,
            url=repo_url,
            branch=branch,
            path=path,
            token=token,
            max_tarball_bytes=runtime.settings.github.max_tarball_bytes,
            max_tarball_decompressed_bytes=runtime.settings.github.max_tarball_decompressed_bytes,
        )
    except GitHubFetchError as exc:
        hint = " A private repo needs an admin, or paste the SKILL.md." if token is None else ""
        raise ToolError(f"{exc}{hint}") from exc
    where = f"{repo_url}/{path}".rstrip("/")
    return bundle, f"{where}@{branch}"


async def _fetch_platform_attachment(
    runtime: McpRuntime, auth: AuthIdentity, http: httpx.AsyncClient, url: str
) -> tuple[bytes, str]:
    """Bytes and filename of a file attached in the caller's own chat platform.

    Discord: its CDN over https. Slack: this server's signed file link, for the
    caller's own workspace, read with that workspace's bot token. Nothing else.
    """
    parsed = urlparse(url)
    if auth.platform == "discord":
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in _DISCORD_ATTACHMENT_HOSTS:
            raise ToolError("attachment_url must be a Discord attachment link.")
        try:
            data = await fetch_attachment(http, url)
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not download the attachment: {exc}") from exc
        return data, unquote(PurePosixPath(parsed.path).name)
    if auth.platform == "slack":
        return await _fetch_slack_attachment(runtime, auth, http, url)
    raise ToolError(
        "Attachments come only from Discord or Slack. Pass skill_md or repo_url instead."
    )


async def _fetch_slack_attachment(
    runtime: McpRuntime, auth: AuthIdentity, http: httpx.AsyncClient, url: str
) -> tuple[bytes, str]:
    root = runtime.settings.mcp.app_root_url
    secret = runtime.settings.mcp.jwt_secret
    prefix = f"{root}{_SLACK_FILE_PATH}" if root else None
    if prefix is None or secret is None or runtime.fernet is None or not url.startswith(prefix):
        raise ToolError("attachment_url must be the link Daimon gave for a Slack file.")
    ref = verify_file_token(
        url.removeprefix(prefix), secret=secret.get_secret_value(), now=int(time.time())
    )
    if ref is None or ref.team_id != auth.external_id:
        raise ToolError("That Slack file link has expired or is not from this workspace.")
    async with runtime.session_factory() as session:
        row = await get_slack_bot_token(session, team_id=ref.team_id)
    if row is None:
        raise ToolError("Daimon is not installed in this Slack workspace any more.")
    try:
        bot_token = decrypt_token(runtime.fernet, row.encrypted_token)
        data, _type, filename = await fetch_slack_file(
            http, bot_token=bot_token, file_id=ref.file_id, max_bytes=MAX_UNCOMPRESSED_BYTES
        )
    except (InvalidToken, httpx.HTTPError) as exc:
        raise ToolError(f"Could not download the Slack file: {exc}") from exc
    return data, filename


async def _add_skill_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    expected_ma_agent_id: str | None,
    skill_md: str | None = None,
    attachment_url: str | None = None,
    repo_url: str | None = None,
    path: str = "",
    branch: str = "main",
    content_hash: str | None = None,
) -> AddSkillResult:
    if sum(source is not None for source in (skill_md, attachment_url, repo_url)) != 1:
        raise ToolError("Pass exactly one of skill_md, attachment_url or repo_url.")
    agent = await resolve_setup_agent(
        runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
    )
    await require_skill_change(runtime, auth, agent, agent_name=agent_name, operation="skill_add")
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            bundle, origin = await _load_bundle(
                runtime,
                auth,
                http,
                skill_md=skill_md,
                attachment_url=attachment_url,
                repo_url=repo_url,
                branch=branch,
                path=path,
            )
        preview = bundle.preview
        if content_hash is None:
            scripts = (
                f" It holds {len(preview.scripts)} runnable file(s)." if preview.scripts else ""
            )
            return AddSkillResult(
                status="preview",
                agent_name=agent_name,
                preview=preview,
                summary=(
                    f"Nothing is added yet. Show the person '{preview.name}': its description, "
                    f"files and runnable files.{scripts} Only after they say yes, call add_skill "
                    f"again with the same arguments and content_hash='{preview.content_hash}'."
                ),
            )
        if content_hash != preview.content_hash:
            raise ToolError(
                "The skill changed since its preview, so nothing was added. Call add_skill "
                "without content_hash to preview it again."
            )
        added = await add_agent_skill(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent=agent,
            agent_name=agent_name,
            bundle=bundle,
            origin=origin,
            added_by_account_id=auth.account_id,
        )
    except DaimonError as exc:
        raise ToolError(str(exc)) from exc
    done = {
        "created": f"Added '{preview.name}' to '{agent_name}'",
        "updated": f"Updated '{preview.name}' on '{agent_name}'",
        "unchanged": f"'{agent_name}' already had this '{preview.name}'",
    }[added.action]
    return AddSkillResult(
        status="added",
        agent_name=agent_name,
        preview=preview,
        added=added,
        summary=f"{done}; it applies from the agent's next message.",
    )


def register_skill_upload_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def add_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        expected_ma_agent_id: str | None = None,
        skill_md: Annotated[
            str | None, Field(description="The full SKILL.md text, frontmatter included.")
        ] = None,
        attachment_url: Annotated[
            str | None,
            Field(description="A .md or .zip the person attached in this chat, by its link."),
        ] = None,
        repo_url: Annotated[
            str | None, Field(description="A GitHub repository holding the skill.")
        ] = None,
        path: Annotated[
            str, Field(description="The skill's folder in repo_url; empty for its only skill.")
        ] = "",
        branch: str = "main",
        content_hash: Annotated[
            str | None,
            Field(description="The preview's content_hash, once the person has confirmed it."),
        ] = None,
    ) -> AddSkillResult:
        """Add a skill to one agent from a pasted SKILL.md, a .md or .zip attached in
        this chat, or one folder of a GitHub repository. Pass exactly one source.

        The first call only previews: name, description, files and the files the
        agent could run. Show that to the person; only when they confirm, call again
        with the same arguments plus the preview's ``content_hash`` to upload it. The
        skill belongs to this agent alone; ``sync_skills`` fills the shared library
        instead, and ``remove_skill`` detaches it. A built-in agent must be forked
        first. Anyone may change an agent no channel uses as its default; otherwise it
        takes a server admin or an admin of every channel using it."""
        return await _add_skill_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            expected_ma_agent_id=expected_ma_agent_id,
            skill_md=skill_md,
            attachment_url=attachment_url,
            repo_url=repo_url,
            path=path,
            branch=branch,
            content_hash=content_hash,
        )

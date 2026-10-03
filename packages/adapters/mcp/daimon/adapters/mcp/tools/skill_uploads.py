"""add_skill: add one pasted, attached or GitHub skill to one agent, after a preview.

``register_skill_upload_tools(mcp, runtime)`` wires the tool; ``_add_skill_impl``
is testable without a FastMCP Context. The checks and the upload live in
``daimon.core.skills``; this module picks the source, gates the caller and
turns refusals into ``ToolError``.

A call without ``content_hash`` only previews. The upload needs the hash that
preview returned, bound to this agent, and it never lands on the model's word
alone: the server confirms only from a chat turn's verified origin whose live
session itself makes ``add_skill`` wait for the person's Approve on the card
(`has_confirmation_gate`). Anything else (tool safety off, an ``agent_chat``
or unattended run, a session whose tools have not caught up with tool safety
yet) adds nothing and points to Add skill in ``/agent-setup``, which previews
and adds on the person's own click. The pin and sharing gates run again on
the fresh agent right before the upload and the attach.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import unquote, urlparse

import anthropic
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import InvalidToken
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import reachability
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools._session_gate import session_asks_first
from daimon.adapters.mcp.tools.agents import (
    _system_agent_rejection,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import (
    get_chat_origin,
    origin_channel_id,
    resolve_setup_agent,
)
from daimon.adapters.mcp.tools.skills import (
    _resolve_sync_token,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.agent_pins import agent_pin_names
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
    fetch_teams_attachment,
    is_teams_download_url,
    repo_origin,
)
from daimon.core.skills.fetch import GitHubFetchError
from daimon.core.skills.ingest import (
    UPLOAD_SUFFIXES,
    SkillBundle,
    SkillPreview,
    bundle_from_markdown,
    bundle_from_upload,
    confirmation_hash,
    require_upload_suffix,
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
    """Its `content_hash` is the one to confirm: this content, for this agent."""
    added: SkillAddResult | None = None
    summary: str


_TOOL_NAME = "add_skill"
_log = structlog.get_logger(__name__)


def _no_card_refusal(agent_name: str, *, deployment_has_cards: bool) -> str:
    why = (
        "this conversation can't show one (it runs without a person, or its session "
        "has not picked the card up yet; the next message does)"
        if deployment_has_cards
        else "this deployment shows none"
    )
    return (
        "Nothing was added. Adding a skill from chat needs the person to press Approve on "
        f"a confirmation card, and {why}. Tell them to run /agent-setup, open {agent_name} "
        "and use Add skill: it shows the same preview and adds it on their own click. "
        "Do not retry."
    )


async def require_skill_change(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent: BetaManagedAgentsAgent,
    *,
    agent_name: str,
    operation: OperationKind,
) -> None:
    """Raise unless the caller may add or remove one of `agent`'s skills.

    A built-in agent is refused; a server admin may change any other; a
    channel admin one of theirs that answers and runs only in their channels
    (`daimon.core.authz.channel_admin_holds`); anyone one
    nobody else uses, read as widely as a key change
    (`daimon.core.agent_reach.WIDE_SHARING_OPERATIONS`).
    """
    rejection = _system_agent_rejection(agent)
    if rejection is not None:
        raise ToolError(rejection)
    facts = await reachability.target_facts(
        runtime,
        auth,
        operation,
        agent_names=(agent_name, *agent_pin_names(agent.name, agent.metadata)),
        ma_agent_id=agent.id,
        is_daimon_managed=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
    )
    if decide_operation(operation, is_admin=auth.is_admin, target=facts) == "allow":
        return
    if facts.runs_unattended_beyond_caller:
        why = "runs unattended (a routine or queued wake) for someone with wider rights"
    elif facts.has_unplaced_run:
        why = reachability.UNPLACED_RUN_REASON
    elif facts.is_local_to_caller_channels:
        why = reachability.NOT_HELD_REASON
    else:
        why = (
            "is used beyond this caller (a default, a bound thread, or someone else's "
            "routine or conversation)"
        )
    raise ToolError(
        f"'{agent_name}' {why}, so changing its skills needs a workspace or server admin, "
        "or an admin of every channel it answers in. Tell the caller an admin can ask "
        "Daimon to make this change. Nothing was changed. Do not retry."
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
        return await asyncio.to_thread(bundle_from_markdown, skill_md), "pasted"
    if attachment_url is not None:
        data, filename = await _fetch_platform_attachment(runtime, auth, http, attachment_url)
        bundle = await asyncio.to_thread(bundle_from_upload, data, filename=filename)
        return bundle, f"attachment {filename}"
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
    return bundle, repo_origin(repo_url, path=path, branch=branch)


async def _fetch_platform_attachment(
    runtime: McpRuntime, auth: AuthIdentity, http: httpx.AsyncClient, url: str
) -> tuple[bytes, str]:
    """Bytes and filename of a file attached in the caller's own chat platform.

    Discord: its CDN over https. Slack: this server's signed file link, for the
    caller's own workspace, read with that workspace's bot token. Teams: the
    file's pre-authorised SharePoint, OneDrive or Graph download link, with no
    credential and no redirect off those hosts. Nothing else.
    """
    parsed = urlparse(url)
    if auth.platform == "discord":
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in _DISCORD_ATTACHMENT_HOSTS:
            raise ToolError("attachment_url must be a Discord attachment link.")
        filename = unquote(PurePosixPath(parsed.path).name)
        require_upload_suffix(filename)
        try:
            data = await fetch_attachment(http, url)
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not download the attachment: {exc}") from exc
        return data, filename
    if auth.platform == "slack":
        return await _fetch_slack_attachment(runtime, auth, http, url)
    if auth.platform == "teams":
        try:
            allowed = is_teams_download_url(httpx.URL(url))
        except httpx.InvalidURL:
            allowed = False
        if not allowed:
            raise ToolError("attachment_url must be a Teams file's SharePoint or OneDrive link.")
        try:
            return await fetch_teams_attachment(http, url)
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not download the attachment: {exc}") from exc
    raise ToolError(
        "Attachments come only from Discord, Slack or Teams. Pass skill_md or repo_url instead."
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
            http,
            bot_token=bot_token,
            file_id=ref.file_id,
            max_bytes=MAX_UNCOMPRESSED_BYTES,
            suffixes=UPLOAD_SUFFIXES,
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
    origin_context_id: str | None = None,
) -> AddSkillResult:
    if sum(source is not None for source in (skill_md, attachment_url, repo_url)) != 1:
        raise ToolError("Pass exactly one of skill_md, attachment_url or repo_url.")
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=agent_name,
        expected_ma_agent_id=expected_ma_agent_id,
        location_channel_id=origin_channel_id(origin),
    )

    async def recheck(fresh: BetaManagedAgentsAgent) -> None:
        # The upload waits for the person's Approve on the card, like the private
        # request forms, so a member inside a pinned agent's channels may add one.
        await require_pin_write_access(runtime, auth, ma_agent=fresh, origin=origin)
        await require_skill_change(
            runtime, auth, fresh, agent_name=agent_name, operation="skill_add"
        )

    await recheck(agent)
    deployment_has_cards = runtime.settings.tool_safety.enabled
    try:
        has_card = deployment_has_cards and await session_asks_first(
            runtime, auth, origin, tool_name=_TOOL_NAME
        )
        if content_hash is not None and not has_card:
            raise ToolError(_no_card_refusal(agent_name, deployment_has_cards=deployment_has_cards))
        async with httpx.AsyncClient(timeout=30.0) as http:
            bundle, source = await _load_bundle(
                runtime,
                auth,
                http,
                skill_md=skill_md,
                attachment_url=attachment_url,
                repo_url=repo_url,
                branch=branch,
                path=path,
            )
        bound = confirmation_hash(bundle.preview, agent_id=agent.id)
        preview = bundle.preview.model_copy(update={"content_hash": bound})
        if content_hash is None:
            scripts = (
                f" It holds {len(preview.scripts)} runnable file(s)." if preview.scripts else ""
            )
            then = (
                f"Only after they say yes, call add_skill again with the same arguments and "
                f"content_hash='{bound}'; they approve it once more on the card."
                if has_card
                else f"To add it they run /agent-setup, open {agent_name} and use Add skill; "
                "adding from chat needs a confirmation card this conversation can't show."
            )
            return AddSkillResult(
                status="preview",
                agent_name=agent_name,
                preview=preview,
                summary=(
                    f"Nothing is added yet. Show the person '{preview.name}': its description, "
                    f"files and runnable files.{scripts} {then}"
                ),
            )
        if content_hash != bound:
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
            origin=source,
            added_by_account_id=auth.account_id,
            recheck=recheck,
        )
    except DaimonError as exc:
        raise ToolError(str(exc)) from exc
    except anthropic.APIError as exc:
        status = getattr(exc, "status_code", None)
        raise ToolError(
            f"Adding the skill failed upstream{f' (HTTP {status})' if status else ''}; it is "
            "not attached. Try again later."
        ) from exc
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
        origin_context_id: str | None = None,
    ) -> AddSkillResult:
        """Add a skill to one agent from a pasted SKILL.md, a .md or .zip attached in
        this chat, or one folder of a GitHub repository. Pass exactly one source.

        The first call only previews: name, description, files and the files the
        agent could run. Show that to the person; only when they confirm, call again
        with the same arguments plus the preview's ``content_hash``, and the person
        approves the upload on a confirmation card. Where this conversation can't
        show one, the preview's summary says to use Add skill in /agent-setup. The skill belongs to
        this agent alone; ``sync_skills`` fills the shared library instead, and
        ``remove_skill`` detaches it. A built-in agent is refused. Anyone may change an
        agent nobody else uses (no default, bound thread, or other people's routine or
        conversation); otherwise it takes a server admin or an admin of every channel
        using it. Pass this turn's ``origin_context_id`` so its channel counts."""
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
            origin_context_id=origin_context_id,
        )

"""add_skill: add one pasted, attached, GitHub or MCP-served skill to one agent, after a preview.

``register_skill_upload_tools(mcp, runtime)`` wires the tool; ``_add_skill_impl``
is testable without a FastMCP Context. The checks and the upload live in
``daimon.core.skills``; this module picks the source, gates the caller and
turns refusals into ``ToolError``.

A call without ``content_hash`` only previews. The upload needs the hash that
preview returned, bound to this agent, and it never lands on the model's word
alone: the server confirms only from a chat turn's verified origin whose live
session itself makes ``add_skill`` wait for the person's Approve on the card
(`has_confirmation_gate`). Where the deployment turned cards off, a chat
preview is recorded instead (`daimon.core.stores.pending_skill_adds`): the
person's next message in that thread confirms it only if the adapter saw an
explicit yes, and only for the turn that message started, for the same agent
and content, once, within the TTL; any other reply cancels it. Anything else
(an ``agent_chat`` or unattended run, a session whose tools have not caught up
with tool safety yet) adds nothing, says which it was, and points to Add skill
in ``/agent-setup``, which previews and adds on the person's own click. The pin
and sharing gates run again on the fresh agent right before the upload and the
attach.
"""

from __future__ import annotations

import asyncio
import ssl
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import unquote, urlparse

import anthropic
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import reachability
from daimon.adapters.mcp.tools._channel_policy import caller_foreign_status, load_read_policy
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._gemini_resources import gemini_skill_host
from daimon.adapters.mcp.tools._openai_resources import (
    gate_view,
    openai_skill_host,
    resolve_host_agent,
)
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.adapters.mcp.tools._words import admin_noun, setup_instruction
from daimon.adapters.mcp.tools.agents import (
    _system_agent_rejection,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.github_connect import (
    _github_connect_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import (
    get_chat_origin,
    origin_channel_id,
    resolve_setup_agent,
)
from daimon.adapters.mcp.tools.skills import (
    _resolve_sync_token,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.adapters.mcp.tools.slack._files import require_file_source, resolve_file_link
from daimon.adapters.mcp.tools.slack._leak_policy import get_destination, is_dm_destination
from daimon.core.agent_mcp_credentials import resolve_agent_mcp_credentials
from daimon.core.agent_pins import agent_pin_names
from daimon.core.config import SignedIdentityServer
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import (
    caller_claims,
    load_signing_key,
    origin_turn_place,
    relay_base,
    sign_identity,
)
from daimon.core.mcp_skills.install import fetch_server_skill, server_access
from daimon.core.operation_policy import OperationKind, decide_operation
from daimon.core.skill_zip import MAX_UNCOMPRESSED_BYTES
from daimon.core.skills.add import (
    SkillAddResult,
    add_agent_skill,
    add_host_agent_skill,
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
    bundle_from_files,
    bundle_from_markdown,
    bundle_from_upload,
    confirmation_hash,
    require_upload_suffix,
)
from daimon.core.slack_files import fetch_slack_file
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.neutral_catalog import CatalogConflict
from daimon.core.stores.pending_skill_adds import (
    PENDING_SKILL_ADD_TTL,
    consume_pending_skill_add,
    record_pending_skill_add,
)
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.errors import MuxError
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from mux.contracts.resources import Agent

__all__ = ["AddSkillResult", "register_skill_upload_tools", "require_skill_change"]

#: The only hosts Discord serves attachments from (as `discord_send`'s allowlist).
_DISCORD_ATTACHMENT_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


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


NoCardReason = Literal[
    "cards_off",
    "agent_key",
    "not_a_chat_turn",
    "no_origin_context",
    "unverified_origin",
    "no_live_session",
    "other_agents_session",
    "session_not_gated",
    "session_unreadable",
]

_PASS_ORIGIN = "Call add_skill again with your origin_context_id."

#: Why no card can show, and what lets the person get one, if anything does.
_NO_CARD: dict[NoCardReason, tuple[str, str | None]] = {
    "cards_off": (
        "approval cards are turned off here (tool safety is disabled); whoever runs this "
        "Daimon install can turn them on",
        None,
    ),
    "agent_key": ("this call runs on an agent key, not in a person's chat", None),
    "not_a_chat_turn": (
        "this call does not come from a chat (a direct connection, for example)",
        None,
    ),
    "no_origin_context": ("the call did not pass its origin_context_id", _PASS_ORIGIN),
    "unverified_origin": (
        "the origin_context_id it passed is not this chat's (expired, malformed, or someone "
        "else's)",
        _PASS_ORIGIN,
    ),
    "no_live_session": ("this thread has no live conversation for the person", None),
    "other_agents_session": (
        "this thread's live conversation runs a different agent from the one answering",
        None,
    ),
    "session_not_gated": (
        "this thread's conversation does not ask before add_skill yet (it started before "
        "approval cards were turned on, or its tools have not been updated)",
        "The person's next message in this thread updates the conversation; preview again "
        "after it.",
    ),
    "session_unreadable": (
        "Daimon could not read this thread's conversation to check for the card",
        "Try again in a moment.",
    ),
}


async def _no_card_reason(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    *,
    origin_context_id: str | None,
) -> NoCardReason | None:
    """None when the person approves the upload on a card, else why no card can show."""
    if not runtime.settings.tool_safety.enabled:
        return "cards_off"
    gap = await session_card_gap(runtime, auth, origin, tool_name=_TOOL_NAME)
    # This call never opts into `require_public_context`, so the gate never
    # returns that reason; narrow it away rather than widening this tool's
    # own, unrelated `NoCardReason` vocabulary.
    assert gap != "source_context_unavailable"
    if gap != "no_origin":
        return gap
    if auth.agent_id is not None:
        return "agent_key"
    if auth.platform is None or auth.chat_agent_id is None:
        return "not_a_chat_turn"
    return "no_origin_context" if not origin_context_id else "unverified_origin"


def _no_card_text(reason: NoCardReason) -> tuple[str, str | None]:
    why, fix = _NO_CARD[reason]
    if reason != "cards_off":
        why = f"approval cards are on, but this conversation can't show one: {why}"
    return why, fix


def _chat_confirms(
    auth: AuthIdentity, origin: TurnOriginRow | None, no_card: NoCardReason | None
) -> bool:
    """Whether this call's person confirms in their next message, cards being off."""
    return (
        no_card == "cards_off"
        and origin is not None
        and auth.agent_id is None
        and auth.chat_agent_id is not None
        and auth.platform_user_id is not None
    )


async def _sender_is_foreign(
    runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow | None
) -> bool | None:
    """Whether the sender is from another organisation: the origin says so, and without
    one (no `origin_context_id` was passed) the person's running turns do. None is not known."""
    if origin is not None:
        return origin.sender_foreign
    if auth.platform != "teams":
        return False
    return await caller_foreign_status(runtime, auth)


def _panel(platform: str | None, agent_name: str, *, foreign: bool | None = False) -> str:
    if foreign:
        return (
            f"{setup_instruction(platform, foreign=True)}; there, open {agent_name} and use Add "
            "skill: it shows the same preview and adds it on their own click"
        )
    return (
        f"{setup_instruction(platform, foreign=foreign)}, open {agent_name} and use Add skill: "
        "it shows the same preview and adds it on their own click"
    )


def _no_card_preview(
    platform: str | None, reason: NoCardReason, agent_name: str, *, foreign: bool | None = False
) -> str:
    why, fix = _no_card_text(reason)
    then = f"{fix} Otherwise they" if fix else "To add it they"
    panel = _panel(platform, agent_name, foreign=foreign)
    return f"Adding it from chat needs a confirmation card, and {why}. {then} {panel}."


def _no_card_refusal(
    platform: str | None, reason: NoCardReason, agent_name: str, *, foreign: bool | None = False
) -> str:
    why, fix = _no_card_text(reason)
    then = f"{fix} Otherwise tell them" if fix else "Tell them"
    end = "" if fix else " Do not retry."
    return (
        "Nothing was added. Adding a skill from chat needs the person to press Approve on a "
        f"confirmation card, and {why}. {then} to "
        f"{_panel(platform, agent_name, foreign=foreign)}.{end}"
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
    a server or workspace admin.
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
    raise ToolError(
        f"Changing '{agent_name}'s skills needs its owner or {admin_noun(auth.platform)}. "
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
    origin_context_id: str | None,
    agent_id: uuid.UUID | None,
    agent_name: str,
    expected_ma_agent_id: str | None,
) -> tuple[SkillBundle, str]:
    """The checked skill and a short origin for the ledger."""
    if skill_md is not None:
        return await asyncio.to_thread(bundle_from_markdown, skill_md), "pasted"
    if attachment_url is not None:
        data, filename = await _fetch_platform_attachment(
            runtime, auth, http, attachment_url, origin_context_id=origin_context_id
        )
        bundle = await asyncio.to_thread(bundle_from_upload, data, filename=filename)
        return bundle, f"attachment {filename}"
    if repo_url is None:
        raise ToolError("Pass exactly one of skill_md, attachment_url or repo_url.")
    token = await _resolve_sync_token(runtime, auth, repo_url, http, agent_id=agent_id)
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
        if exc.status_code == 404 and token is None:
            if agent_id is not None and origin_context_id is not None:
                await _github_connect_impl(
                    runtime,
                    auth,
                    origin_context_id=origin_context_id,
                    agent_name=agent_name,
                    expected_ma_agent_id=expected_ma_agent_id,
                    repo_name=repo_url.removeprefix("https://github.com/").removesuffix(".git"),
                    required_ability="read",
                )
                raise ToolError(
                    f"No active GitHub App grant for {repo_url!r}. "
                    "The Connect GitHub button was posted."
                ) from exc
            raise ToolError(
                f"No active GitHub App grant for {repo_url!r}. "
                "Ask an admin to use Connect GitHub for this agent and repo."
            ) from exc
        raise ToolError(str(exc)) from exc
    return bundle, repo_origin(repo_url, path=path, branch=branch)


async def _fetch_platform_attachment(
    runtime: McpRuntime,
    auth: AuthIdentity,
    http: httpx.AsyncClient,
    url: str,
    *,
    origin_context_id: str | None,
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
        return await _fetch_slack_attachment(
            runtime, auth, http, url, origin_context_id=origin_context_id
        )
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
    raise ToolError("Attachments aren't available here. Pass skill_md or repo_url instead.")


async def _fetch_slack_attachment(
    runtime: McpRuntime,
    auth: AuthIdentity,
    http: httpx.AsyncClient,
    url: str,
    *,
    origin_context_id: str | None,
) -> tuple[bytes, str]:
    """The linked file, once the caller is shown to be able to read it.

    The link alone is a bearer token any turn in the workspace may hold, so
    the file must be shared where this caller can read from this turn.
    """
    ref = (
        resolve_file_link(runtime, url, team_id=auth.external_id)
        if auth.external_id is not None
        else "not_a_link"
    )
    if ref == "rejected":
        raise ToolError("That Slack file link has expired or is not from this workspace.")
    if isinstance(ref, str) or runtime.fernet is None or auth.platform_user_id is None:
        raise ToolError("attachment_url must be the link Daimon gave for a Slack file.")
    async with runtime.session_factory() as session:
        row = await get_slack_bot_token(session, team_id=ref.team_id)
    if row is None:
        raise ToolError("Daimon is not installed in this Slack workspace any more.")
    client = await slack_web_client(runtime, team_id=ref.team_id)
    destination = await get_destination(runtime, auth, now=datetime.now(tz=UTC))
    await require_file_source(
        client,
        file_id=ref.file_id,
        requester_id=auth.platform_user_id,
        read_policy=await load_read_policy(runtime, auth, origin_context_id=origin_context_id),
        dm_ok=is_dm_destination(destination),
    )
    try:
        data, _type, filename = await fetch_slack_file(
            http,
            bot_token=str(client.token),
            file_id=ref.file_id,
            max_bytes=MAX_UNCOMPRESSED_BYTES,
            suffixes=UPLOAD_SUFFIXES,
        )
    except httpx.HTTPError as exc:
        raise ToolError(f"Could not download the Slack file: {exc}") from exc
    return data, filename


def _mcp_http(verify: bool | ssl.SSLContext) -> httpx.AsyncClient:
    """The client an MCP server's skill is read with: the configured URL, no redirects."""
    return httpx.AsyncClient(timeout=30.0, follow_redirects=False, verify=verify)


async def _load_mcp_skill(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    *,
    agent: BetaManagedAgentsAgent,
    agent_name: str,
    server: str,
    skill: str,
) -> tuple[SkillBundle, str]:
    """The skill `server` (one of the agent's MCP servers) serves, checked, and its origin.

    A relay server gets the relay's signed identity for the person asking: the
    agent, the workspace, the person and, from a chat turn, the channel.
    """
    url = next((s.url for s in agent.mcp_servers if s.name == server), None)
    if url is None:
        raise ToolError(f"'{agent_name}' has no MCP server named '{server}'.")
    ledger_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id)
    credentials = (
        await resolve_agent_mcp_credentials(
            sessionmaker=runtime.session_factory,
            fernet=runtime.fernet,
            tenant_id=auth.tenant_id,
            agent_id=ledger_id,
        )
        if runtime.fernet is not None
        else ()
    )
    identity = runtime.settings.mcp_identity
    public_url = runtime.settings.mcp.public_url
    key = (
        load_signing_key(identity.signing_key.get_secret_value())
        if identity.signing_key is not None
        else None
    )
    # As the relay: a sender from another organisation is not one of the workspace's people.
    foreign = await _sender_is_foreign(runtime, auth, origin) is not False

    def relay_token(configured: SignedIdentityServer) -> str | None:
        workspace = f"{auth.platform}:{auth.external_id}"
        if key is None or (
            configured.workspaces is not None and workspace not in configured.workspaces
        ):
            return None
        claims = caller_claims(
            issuer=configured.issuer,
            audience=configured.audience,
            agent_id=str(ledger_id),
            platform=auth.platform or "",
            workspace_id=auth.external_id,
            platform_user_id=None if foreign else auth.platform_user_id,
            agent_name=agent_name,
            place=origin_turn_place(origin) if origin is not None and not foreign else None,
        )
        return sign_identity(claims, key=key, now=datetime.now(UTC))

    access = server_access(
        server=server,
        url=url,
        credentials=credentials,
        relay_base=relay_base(str(public_url)) if public_url is not None else None,
        relay_servers={configured.name: configured for configured in identity.servers},
        relay_token=relay_token,
    )
    async with _mcp_http(access.verify) as http:
        fetched = await fetch_server_skill(http, access.endpoint, server=server, skill=skill)
    bundle = await asyncio.to_thread(bundle_from_files, fetched.files)
    return bundle, fetched.origin(server)


def _upstream_recovery(chat_confirms: bool) -> str:
    # A chat approval was used up above; only a card approval can be retried as is.
    if chat_confirms:
        return (
            "Ask the person to preview again and reply `yes` to the new preview before "
            "trying again."
        )
    return "Try again later."


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
    mcp_server: str | None = None,
    mcp_skill: str | None = None,
    content_hash: str | None = None,
    origin_context_id: str | None = None,
) -> AddSkillResult:
    if sum(source is not None for source in (skill_md, attachment_url, repo_url, mcp_server)) != 1:
        raise ToolError("Pass exactly one of skill_md, attachment_url, repo_url or mcp_server.")
    if (mcp_server is None) != (mcp_skill is None):
        raise ToolError("Pass mcp_server and mcp_skill together.")
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    # Gemini uses a separate default-off operator grant and the catalog.
    # Otherwise retain the existing OpenAI/Anthropic selection unchanged.
    hosted = await gemini_skill_host(
        runtime, auth, origin, capability=runtime.gemini_skill_capability, operation="skill-add"
    )
    if hosted is None:
        hosted = await openai_skill_host(runtime, auth, origin, operation="skill-add")
    mcp_agent: BetaManagedAgentsAgent | None = None
    if hosted is None:
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
        mcp_agent = agent
        target_id = agent.id
        github_agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))

        async def add(bundle: SkillBundle, source: str) -> SkillAddResult:
            return await add_agent_skill(
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
    else:
        if mcp_server is not None:
            raise ToolError(f"'{agent_name}' cannot take a skill from an MCP server.")
        host = hosted
        # The same identity and channel-isolation rules as resolve_setup_agent.
        view = await resolve_host_agent(
            runtime,
            auth,
            host,
            name=agent_name,
            expected_ma_agent_id=expected_ma_agent_id,
            location_channel_id=origin_channel_id(origin),
        )

        async def neutral_recheck(fresh: Agent) -> None:
            # The same pin and skill-change policy as the Anthropic path, on the
            # agent as the host reads it now.
            gate = gate_view(fresh)
            await require_pin_write_access(runtime, auth, ma_agent=gate, origin=origin)
            await require_skill_change(
                runtime, auth, gate, agent_name=agent_name, operation="skill_add"
            )

        await neutral_recheck(view.revision.agent)
        target_id = view.id
        github_agent_id = None
        catalog_id = view.revision.catalog_id

        async def add(bundle: SkillBundle, source: str) -> SkillAddResult:
            del source  # the host retains its own upload provenance
            return await add_host_agent_skill(
                host, catalog_id=catalog_id, bundle=bundle, recheck=neutral_recheck
            )

    chat_confirms = False
    try:
        no_card = await _no_card_reason(runtime, auth, origin, origin_context_id=origin_context_id)
        if no_card is not None:
            _log.info(
                "add_skill.no_card",
                reason=no_card,
                confirm=content_hash is not None,
                agent_name=agent_name,
            )
        chat_confirms = _chat_confirms(auth, origin, no_card)
        if content_hash is not None and no_card is not None and not chat_confirms:
            foreign = await _sender_is_foreign(runtime, auth, origin)
            raise ToolError(_no_card_refusal(auth.platform, no_card, agent_name, foreign=foreign))
        if mcp_server is not None and mcp_skill is not None and mcp_agent is not None:
            bundle, source = await _load_mcp_skill(
                runtime,
                auth,
                origin,
                agent=mcp_agent,
                agent_name=agent_name,
                server=mcp_server,
                skill=mcp_skill,
            )
        else:
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
                    origin_context_id=origin_context_id,
                    agent_id=github_agent_id,
                    agent_name=agent_name,
                    expected_ma_agent_id=target_id if hosted is None else None,
                )
        bound = confirmation_hash(bundle.preview, agent_id=target_id)
        preview = bundle.preview.model_copy(update={"content_hash": bound})
        if content_hash is None:
            scripts = (
                f" It holds {len(preview.scripts)} runnable file(s)." if preview.scripts else ""
            )
            if chat_confirms:
                assert origin is not None
                async with runtime.session_factory.begin() as session:
                    await record_pending_skill_add(
                        session,
                        origin=origin,
                        ma_agent_id=target_id,
                        content_hash=bound,
                        now=datetime.now(UTC),
                    )
                minutes = int(PENDING_SKILL_ADD_TTL.total_seconds() // 60)
                then = (
                    "To add it, they reply with just `yes` as their next message in this "
                    f"thread, within {minutes} minutes; any other reply cancels it. When "
                    "they reply, call add_skill again with the same arguments, "
                    f"content_hash='{bound}' and your origin_context_id. Daimon checks "
                    "their reply itself."
                )
            elif no_card is None:
                then = (
                    f"Only after they say yes, call add_skill again with the same arguments and "
                    f"content_hash='{bound}'; they approve it once more on the card."
                )
            else:
                foreign = await _sender_is_foreign(runtime, auth, origin)
                then = _no_card_preview(auth.platform, no_card, agent_name, foreign=foreign)
            return AddSkillResult(
                status="preview",
                agent_name=agent_name,
                preview=preview,
                summary=(
                    f"Nothing is added yet. Show the person '{preview.name}' for agent "
                    f"'{agent_name}': its description, "
                    f"files and runnable files.{scripts} {then}"
                ),
            )
        if content_hash != bound:
            raise ToolError(
                "The skill changed since its preview, so nothing was added. Call add_skill "
                "without content_hash to preview it again."
            )
        if chat_confirms:
            assert origin is not None
            async with runtime.session_factory.begin() as session:
                confirmed = await consume_pending_skill_add(
                    session,
                    origin=origin,
                    ma_agent_id=target_id,
                    content_hash=bound,
                    now=datetime.now(UTC),
                )
            if not confirmed:
                raise ToolError(
                    "Nothing was added: this message is not the person replying "
                    "`yes` to this preview (they have not replied yet, replied something "
                    "else, it expired, a newer preview replaced it, or it was already used). "
                    "Do not retry this confirmation; preview it again if they ask."
                )
        added = await add(bundle, source)
    except DaimonError as exc:
        raise ToolError(str(exc)) from exc
    except anthropic.APIError as exc:
        status = getattr(exc, "status_code", None)
        raise ToolError(
            f"Adding the skill failed upstream{f' (HTTP {status})' if status else ''}; it is "
            f"not attached. {_upstream_recovery(chat_confirms)}"
        ) from exc
    except CatalogConflict as exc:
        raise ToolError(
            f"'{agent_name}' changed while the skill was being added; it is not attached. "
            "Preview it again if they still want it."
        ) from exc
    except MuxError as exc:
        # The resource host's own errors; their text is never relayed.
        raise ToolError(
            "Adding the skill failed upstream; it is not attached. "
            f"{_upstream_recovery(chat_confirms)}"
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
        mcp_server: Annotated[
            str | None,
            Field(description="One of the agent's MCP servers, by name, that serves the skill."),
        ] = None,
        mcp_skill: Annotated[
            str | None, Field(description="The skill's name or skill:// URI on mcp_server.")
        ] = None,
        content_hash: Annotated[
            str | None,
            Field(description="The preview's content_hash, once the person has confirmed it."),
        ] = None,
        origin_context_id: str | None = None,
    ) -> AddSkillResult:
        """Add a skill to one agent from a pasted SKILL.md, a .md or .zip attached in
        this chat, one folder of a GitHub repository, or a skill one of the agent's MCP
        servers serves (``mcp_server`` with ``mcp_skill``). Pass exactly one source.

        The first call only previews: name, description, files and the files the
        agent could run. Show that to the person; only when they confirm, call again
        with the same arguments plus the preview's ``content_hash``, and the person
        approves the upload on a confirmation card. Where this deployment turned cards
        off, the person confirms by replying just ``yes`` in this thread instead: call
        again only in the turn their reply starts, with that turn's
        ``origin_context_id``; Daimon checks the reply itself. Where
        neither can happen, the preview's summary says why and points to the Add skill option in
        the agents setup panel (the setup command, see ``words.setup`` in
        ``<turn_controls>``). The skill belongs to this agent alone; ``sync_skills``
        fills the shared library instead, and ``remove_skill`` detaches it. A built-in
        agent is refused.
        Changing skills takes an admin. Pass this turn's
        ``origin_context_id`` so its
        channel counts."""
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
            mcp_server=mcp_server,
            mcp_skill=mcp_skill,
            content_hash=content_hash,
            origin_context_id=origin_context_id,
        )

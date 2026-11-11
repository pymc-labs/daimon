"""Ask helper agents for help inside this turn (`daimon.core.helpers`).

The tools only record asks and read replies; the process running the asking
turn runs the helpers. A wait returns before an MCP client's call timeout, and
the agent waits again with `wait_for_helpers`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._rule_view import load_isolation_view
from daimon.adapters.mcp.tools.setup_target import refuse_helper, require_turn_origin
from daimon.core.anthropic_guard import anthropic_disabled
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.helpers import MAX_ASKS
from daimon.core.stores import helper_asks
from daimon.core.stores.agent_helpers import list_helper_ids
from daimon.core.stores.domain import HelperAskRow, HelperAskStatus, TurnOriginRow
from daimon.core.stores.file_uploads import list_landed_uploads
from daimon.core.stores.selfhosted_resources import list_live_agent_facts
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

#: Longest one call waits for replies: well inside an MCP client's call timeout.
WAIT_S = 50.0
_POLL_S = 1.0
#: A longer reply is cut, so one helper cannot fill the asking agent's context.
MAX_REPLY_CHARS = 20_000
MAX_MESSAGE_CHARS = 20_000


class HelperAgent(BaseModel):
    agent_id: str
    name: str
    purpose: str | None = None


class HelperList(BaseModel):
    helpers: list[HelperAgent]


class HelperFile(BaseModel):
    id: str
    name: str
    size: int
    url: str


class HelperAnswer(BaseModel):
    ask_id: str
    helper: str
    status: HelperAskStatus
    reply: str | None = None
    files: list[HelperFile] = []


class HelperAnswers(BaseModel):
    answers: list[HelperAnswer]


def _answer(ask: HelperAskRow, app_root: str | None) -> HelperAnswer:
    reply = ask.reply
    if reply is not None and len(reply) > MAX_REPLY_CHARS:
        reply = reply[:MAX_REPLY_CHARS] + "\n[reply cut]"
    files = [
        HelperFile(
            id=f.id,
            name=f.name,
            size=f.size,
            url=f"{app_root}/helper-files/{ask.files_token}/{f.id}",
        )
        for f in ask.reply_files
        if app_root is not None and ask.files_token is not None
    ]
    return HelperAnswer(
        ask_id=str(ask.id), helper=ask.helper_name, status=ask.status, reply=reply, files=files
    )


async def _roster(
    runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow
) -> list[HelperAgent]:
    """The answering agent's helpers that are live and may be used where it runs."""
    async with runtime.session_factory() as session:
        ids = await list_helper_ids(
            session, tenant_id=auth.tenant_id, agent_id=origin.responder_ma_agent_id
        )
    if not ids:
        return []
    if auth.anthropic_disabled or anthropic_disabled():
        # A self-hosted conversation: its agents are the tenant's own records.
        async with runtime.session_factory() as session:
            agents = await list_live_agent_facts(session, tenant_id=auth.tenant_id)
    else:
        agents = await list_agents_by_tenant(
            runtime.client, tenant_id=auth.tenant_id, records=runtime.session_factory
        )
    view = await load_isolation_view(
        runtime, auth, agents=agents, location_channel_id=origin.parent_channel_id
    )
    live = {agent.id: agent for agent in agents if agent.archived_at is None}
    roster: list[HelperAgent] = []
    for helper_id in ids:
        agent = live.get(helper_id)
        name = agent.metadata.get(MA_METADATA_KEY_NAME) if agent is not None else None
        if agent is None or not name or not view.sees_agent(agent):
            continue
        roster.append(HelperAgent(agent_id=agent.id, name=name, purpose=agent.description or None))
    return roster


async def _wait(
    runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow, ids: list[uuid.UUID]
) -> list[HelperAnswer]:
    until = monotonic() + WAIT_S
    while True:
        async with runtime.session_factory() as session:
            asks = await helper_asks.list_asks(
                session, tenant_id=auth.tenant_id, origin_id=origin.id, ids=ids
            )
        if len(asks) < len(set(ids)):
            raise ToolError("Use the ask ids ask_helper gave you for this message.")
        if all(ask.status not in helper_asks.OPEN for ask in asks) or monotonic() >= until:
            return [_answer(ask, runtime.settings.mcp.app_root_url) for ask in asks]
        await asyncio.sleep(_POLL_S)


async def _list_helpers_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, origin_context_id: str
) -> HelperList:
    origin = refuse_helper(await require_turn_origin(runtime, auth, origin_context_id))
    return HelperList(helpers=await _roster(runtime, auth, origin))


async def _ask_helper_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    helper: str,
    message: str,
    wait: bool,
    files: list[str] | None = None,
) -> HelperAnswer:
    origin = refuse_helper(await require_turn_origin(runtime, auth, origin_context_id))
    if not origin.serves_helpers:
        raise ToolError("Helpers can't be asked from this conversation.")
    roster = await _roster(runtime, auth, origin)
    chosen = next((h for h in roster if helper.strip() in (h.agent_id, h.name)), None)
    if chosen is None:
        names = ", ".join(h.name for h in roster)
        raise ToolError(
            f"That agent is not one of your helpers here. Your helpers: {names}."
            if names
            else "You have no helpers here."
        )
    if not message.strip():
        raise ToolError("Say what the helper should do.")
    handles = list(dict.fromkeys(h.strip() for h in files or []))
    if len(handles) > helper_asks.MAX_FILES:
        raise ToolError(f"Give a helper at most {helper_asks.MAX_FILES} files per ask.")
    if handles and runtime.settings.mcp.app_root_url is None:
        raise ToolError("Files can't be passed to helpers here: Daimon has no public URL.")
    async with runtime.session_factory.begin() as session:
        if await helper_asks.count_asks(session, origin_id=origin.id) >= MAX_ASKS:
            raise ToolError(f"You asked helpers {MAX_ASKS} times for this message. Answer now.")
        landed = await list_landed_uploads(session, tenant_id=auth.tenant_id, handle_ids=handles)
        missing = set(handles) - {f.id for f in landed}
        if missing:
            raise ToolError(
                f"No uploaded file with handle id {', '.join(sorted(missing))}. Mint one with "
                "create_file_upload_url and PUT the file first."
            )
        ask = await helper_asks.create_ask(
            session,
            tenant_id=auth.tenant_id,
            origin_id=origin.id,
            helper_agent_id=chosen.agent_id,
            helper_name=chosen.name,
            message=message,
            now=datetime.now(UTC),
            files=landed,
        )
    if not wait:
        return _answer(ask, runtime.settings.mcp.app_root_url)
    [answer] = await _wait(runtime, auth, origin, [ask.id])
    return answer


async def _wait_for_helpers_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, origin_context_id: str, ask_ids: list[str]
) -> HelperAnswers:
    origin = refuse_helper(await require_turn_origin(runtime, auth, origin_context_id))
    try:
        ids = [uuid.UUID(ask_id) for ask_id in ask_ids]
    except ValueError as exc:
        raise ToolError("Use the ask ids ask_helper gave you for this message.") from exc
    if not ids:
        raise ToolError("Name the asks to wait for.")
    return HelperAnswers(answers=await _wait(runtime, auth, origin, ids))


def register_helper_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_helpers(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
    ) -> HelperList:
        """The agents you may ask for help during this turn with `ask_helper`: id, name, purpose."""
        return await _list_helpers_impl(
            runtime, await _auth(ctx), origin_context_id=origin_context_id
        )

    @mcp.tool
    async def ask_helper(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        helper: Annotated[str, Field(description="A helper's name or agent id from list_helpers.")],
        message: Annotated[
            str,
            Field(
                description=(
                    "Everything the helper needs: the task, the facts and what to send back. "
                    "It can't see this conversation's history or files you don't pass."
                ),
                max_length=MAX_MESSAGE_CHARS,
            ),
        ],
        wait: Annotated[
            bool,
            Field(description="False to start it and ask other helpers before waiting."),
        ] = True,
        files: Annotated[
            list[str] | None,
            Field(description="Handle ids of files to pass to the helper (see below)."),
        ] = None,
    ) -> HelperAnswer:
        """Ask one of your helpers (`list_helpers`) to do part of this turn's work, on its own model, and get its reply.

        It works for the same person in this conversation, with its own tools, keys and
        rules; nothing it writes is posted. Asking the same helper again continues its
        conversation in this thread. While `status` is `queued` or `running`, call
        `wait_for_helpers` with the `ask_id`. Helpers stop when your turn ends, so wait
        for their replies before you answer.

        To pass it files: for each, call `create_file_upload_url`, PUT the file with the
        curl it returns, and pass the handle id in `files`. Files the helper sends back
        are in the answer's `files`: download one with `curl -sS -o <name> "<url>"`, or
        pass its `id` to another helper's `files`. Deliver one to the person as you would
        a file you made. The links expire an hour after the ask.
        """  # noqa: E501
        return await _ask_helper_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            helper=helper,
            message=message,
            wait=wait,
            files=files,
        )

    @mcp.tool
    async def wait_for_helpers(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        ask_ids: Annotated[list[str], Field(description="ask_id values from ask_helper.")],
    ) -> HelperAnswers:
        """Wait for helpers' replies; returns when all of them finish or after about a minute. Call again for any still `queued` or `running`."""  # noqa: E501
        return await _wait_for_helpers_impl(
            runtime, await _auth(ctx), origin_context_id=origin_context_id, ask_ids=ask_ids
        )

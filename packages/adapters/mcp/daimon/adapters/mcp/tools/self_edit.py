"""MCP tools for an agent to edit its own ``agent_files`` from inside an MA turn.

Identity is read server-side from the JWT claims via ``_auth(ctx)``.
Cross-agent isolation is enforced by the ``agent_files`` composite key
``(tenant_id, agent_id, key)`` at the store layer.

All four tools are tagged ``agent-chat`` and are therefore visible only
to a session whose token carries an agent identity — the same identity
their ``_require_agent_id`` precondition needs to succeed. An ordinary
chat session never discovers this group; chat users request GitHub App access
with ``request_github_access`` and manage keys with ``request_agent_key``,
``list_agent_keys`` and ``remove_agent_key``.

``register_self_edit_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools._words import admin_noun
from daimon.adapters.mcp.tools.agent_chat import (
    _resolve_ma_agent,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.env_file import (
    MEMBER_SECRET_SUFFIX_HINT,
    env_alias_shadowed,
    env_name_problem,
    env_shadow_phrase,
)
from daimon.core.errors import StoreError
from daimon.core.operation_policy import TargetFacts, decide_operation, needs_reachability_read
from daimon.core.stores.agent_files import (
    delete_agent_file,
    get_agent_file,
    list_agent_files,
    lock_agent_keys,
    put_agent_file_if_unchanged,
)
from daimon.core.stores.domain import AgentFileRow
from daimon.core.stores.scoped_config_read import is_agent_shared_for_key_changes
from daimon.core.turn_keys import list_turn_key_names
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

logger = structlog.get_logger()


def _require_agent_id(auth: AuthIdentity) -> uuid.UUID:
    """Return ``auth.agent_id`` or raise ``ToolError`` if absent."""
    if auth.agent_id is None:
        raise ToolError("agent_id missing — this token is not an agent's")
    return auth.agent_id


async def _self_write_file_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
    content: str,
) -> AgentFileRow:
    agent_id = _require_agent_id(auth)
    await require_pin_write_access(
        runtime, auth, ma_agent=lambda: _resolve_ma_agent(runtime, auth), origin=None
    )
    # An agent key is always a member here, so the value's name must be one a
    # member may write: a credential name, never a tool-control or redirect
    # name. The store enforces the hard-deny layer again under this.
    # Empty key: let the store speak ("key must not be empty"); the name policy
    # only has something to say about a non-empty name.
    name_problem = env_name_problem(key, is_admin=False) if key else None
    if name_problem == "bad_name":
        raise ToolError(
            "key must match [A-Za-z_][A-Za-z0-9_]* "
            "(letters, digits, underscores; must not start with a digit)"
        )
    if name_problem == "reserved_name":
        raise ToolError(f"{key} is a reserved name: it changes how the agent's tools run.")
    if name_problem == "not_credential_name":
        raise ToolError(
            f"{key} is not a secret name an agent can store. Use a name ending in "
            f"{MEMBER_SECRET_SUFFIX_HINT}; identity, account, region and URL names "
            "need an admin to add them through a form."
        )
    # Add-only, like a member's form: an agent key never overwrites a stored
    # key (the replacement gate is for people) and never adds a name a tool
    # reads as one already held (GH_TOKEN beside GITHUB_TOKEN). The insert's
    # `ON CONFLICT DO NOTHING` serializes writers of the same name; the
    # agent's key-set lock, taken before the alias read, serializes writers
    # of different names in one alias group.
    try:
        async with runtime.session_factory.begin() as session:
            await lock_agent_keys(session, tenant_id=auth.tenant_id, agent_id=agent_id)
            shadowed = (
                env_alias_shadowed(
                    key,
                    await list_turn_key_names(session, tenant_id=auth.tenant_id, agent_id=agent_id),
                )
                if key
                else None
            )
            if shadowed is not None:
                raise ToolError(
                    f"Adding {key} {env_shadow_phrase(key, shadowed)}. Nothing was saved. "
                    f"Ask a person to replace {shadowed} through a form."
                )
            row = await put_agent_file_if_unchanged(
                session,
                tenant_id=auth.tenant_id,
                agent_id=agent_id,
                key=key,
                content=content,
                set_by_account_id=auth.account_id,
                expected_updated_at=None,
            )
            if row is None:
                raise ToolError(
                    f"{key} is already set, and an agent cannot replace a stored key. "
                    f"Nothing was saved. Ask a person to replace {key} through a form."
                )
    except StoreError as e:
        logger.warning(
            "self_write_file outcome=store_error agent=%s key=%s",
            agent_id,
            key,
        )
        raise ToolError(str(e)) from e
    logger.info("self_write_file outcome=success agent=%s key=%s", agent_id, key)
    return row


REDACTED_VALUE = (
    "[redacted: values are in the .env file in your project files, never in tool output]"
)
LISTED_VALUE = "[not listed: values are in the .env file in your project files]"


def _withhold_value(row: AgentFileRow) -> AgentFileRow:
    """Keep every value out of tool output.

    Nothing records whether a person submitted a value through a credential
    form or the agent wrote it itself: both carry the requester's account id.
    So no value comes back. The sandbox already mounts them in the session
    `.env`, and a tool result would put a credential in the model's context
    and transcript.
    """
    return row.model_copy(update={"content": REDACTED_VALUE})


async def _self_read_file_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
) -> AgentFileRow | None:
    agent_id = _require_agent_id(auth)
    async with runtime.session_factory() as session:
        row = await get_agent_file(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            key=key,
        )
    logger.info(
        "self_read_file outcome=%s agent=%s key=%s",
        "hit" if row is not None else "miss",
        agent_id,
        key,
    )
    return None if row is None else _withhold_value(row)


async def _self_list_files_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[AgentFileRow]:
    agent_id = _require_agent_id(auth)
    async with runtime.session_factory() as session:
        rows = await list_agent_files(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
        )
    logger.info("self_list_files outcome=success agent=%s count=%d", agent_id, len(rows))
    # Keys and metadata only: no value, however it was written.
    return [row.model_copy(update={"content": LISTED_VALUE}) for row in rows]


async def _require_member_may_remove(
    runtime: McpRuntime, auth: AuthIdentity, *, agent_id: uuid.UUID, key: str
) -> None:
    """Refuse an agent-key delete that `remove_agent_key` would refuse a member.

    An agent key is a member, whoever owns it. Without this, delete-then-add
    replaces a key a person set (and delete GITHUB_TOKEN, add GH_TOKEN dodges
    the alias rule), and removal skips the admin gate `remove_agent_key`
    applies on the built-in agent and on any agent that answers somewhere in
    the tenant. An agent that no longer resolves fails closed.
    """
    agent = await find_agent_by_derived_uuid(
        runtime.client, tenant_id=auth.tenant_id, agent_id=agent_id
    )
    refusal = ToolError(
        f"Removing {key} needs {admin_noun(auth.platform)}: this agent is shared, and "
        "an agent key acts as a member. Nothing was removed. Ask a person to remove "
        f"{key} with remove_agent_key."
    )
    if agent is None:
        raise refusal
    is_daimon_managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    reachable = False
    if needs_reachability_read("key_remove", is_admin=False, is_daimon_managed=is_daimon_managed):
        async with runtime.session_factory() as session:
            reachable = await is_agent_shared_for_key_changes(
                session,
                tenant_id=auth.tenant_id,
                agent_names=(agent.name, str(agent.metadata.get(MA_METADATA_KEY_NAME) or "")),
                ma_agent_id=str(agent.id),
                default=runtime.deployment_default,
                caller_account_id=auth.account_id,
                caller_platform_user_id=auth.platform_user_id,
            )
    outcome = decide_operation(
        "key_remove",
        is_admin=False,
        target=TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=reachable),
    )
    if outcome != "allow":
        raise refusal


async def _self_delete_file_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
) -> dict[str, object]:
    agent_id = _require_agent_id(auth)
    await require_pin_write_access(
        runtime, auth, ma_agent=lambda: _resolve_ma_agent(runtime, auth), origin=None
    )
    await _require_member_may_remove(runtime, auth, agent_id=agent_id, key=key)
    # delete_agent_file is silently idempotent at the store layer.
    async with runtime.session_factory.begin() as session:
        await delete_agent_file(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            key=key,
        )
    logger.info("self_delete_file outcome=success agent=%s key=%s", agent_id, key)
    return {"deleted": True, "key": key}


def register_self_edit_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    """Register the four agent file tools.

    All four carry the ``agent-chat`` tag, so the ``agent-chat`` ``Visibility``
    baseline in ``server.py`` hides them by default, and the identity
    middleware's narrowing reveals them only when the token carries an
    ``agent_id`` claim — the same precondition ``_require_agent_id`` enforces
    at the impl layer. A session with no agent identity (e.g. an ordinary
    chat turn) never sees this group; chat users manage keys with ``request_agent_key``,
    ``list_agent_keys`` and ``remove_agent_key``.
    """

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def self_write_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
        content: str,
    ) -> AgentFileRow:
        """Add a per-agent key under `key`. Add-only: never replaces a stored key.

        Stored in your private agent_files namespace; isolated from other agents.
        A key that is already set, or a name your tools read as the same
        credential as one already set (`GITHUB_TOKEN` beside `GH_TOKEN`), is
        refused; a person replaces those through a form.
        Every entry is exported into your sandbox's `.env`, so `key` must be a
        credential name: upper-case, ending in `_KEY`, `_TOKEN`, `_SECRET`,
        `_PASSWORD` or similar (or `GH_TOKEN`/`GITHUB_TOKEN`). Tool-control and
        endpoint names (`PATH`, `LD_PRELOAD`, `TAR_OPTIONS`, `*_BASE_URL`,
        `*_URL` …) are refused.
        """
        return await _self_write_file_impl(runtime, await _auth(ctx), key=key, content=content)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def self_read_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
    ) -> AgentFileRow | None:
        """Check a per-agent file by `key`. Returns null if no file exists at that key.

        The value is always redacted: use it from the session `.env` without
        echoing it. The result shows when it was last set.
        """
        return await _self_read_file_impl(runtime, await _auth(ctx), key=key)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def self_list_files(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> list[AgentFileRow]:
        """List all keys + metadata (no values) in your private agent_files namespace."""
        return await _self_list_files_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def self_delete_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
    ) -> dict[str, object]:
        """Delete a per-agent key by `key`. Idempotent — succeeds whether or not it existed.

        Refused on the built-in agent and on any agent that answers somewhere in
        the workspace: removing a key there takes it from everyone, so a person
        with admin rights removes it with `remove_agent_key`.
        """
        return await _self_delete_file_impl(runtime, await _auth(ctx), key=key)

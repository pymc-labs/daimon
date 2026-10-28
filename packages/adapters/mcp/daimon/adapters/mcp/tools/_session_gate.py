"""Whether a chat turn's session made one of daimon's tools wait for the person's Approve.

`add_skill` and the publish tools (`PUBLISH_TOOLS`) run on that card, never on
the model's word: MA runs a call held on `always_ask` only once the person
approves it, so a call from a verified origin whose live session holds the tool
there was approved.
"""

from __future__ import annotations

from typing import Literal

import anthropic
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.resource_ports import mcp_scope, retrieve_session
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.agent_mcp_credentials import resolve_hidden_mcp_server_names
from daimon.core.channel_backend import backend_selection_enabled
from daimon.core.config import get_repo_memory_settings
from daimon.core.confirmation import backend_switch_tools
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_personal_servers import visible_tools
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.session_seal import session_facts
from daimon.core.session_snapshot import hash_tools, session_tools
from daimon.core.stores.domain import ThreadSessionRow, TurnOriginRow
from daimon.core.stores.lead_intake import confirmation_tools
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    WATCH_CONFIRMED_TOOLS,
    has_confirmation_gate,
    microsoft_send_tools,
)
from daimon.core.turn.approvals import daimon_rule_tools_for
from mux.errors import ScopeViolation

_log = structlog.get_logger(__name__)

CardGap = Literal[
    "no_origin",
    "no_live_session",
    "other_agents_session",
    "session_not_gated",
    "session_unreadable",
    "source_context_unavailable",
]
"""Why a call's session can't show the card: no verified chat origin; no live
session in the origin's thread for this caller; that session runs another
agent than the turn's responder; it does not hold the tool on `always_ask`;
MA could not be read."""


async def session_card_gap(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    *,
    tool_name: str,
    require_public_context: bool = False,
) -> CardGap | None:
    """None when this call's session waits for the person's Approve, else why it does not.

    Read from the session itself: the verified origin's live session must run
    the origin's responder and hold `tool_name` on `always_ask`, as MA reports
    it or, failing that, as daimon recorded sending it (`_recorded_as_gated`).
    An `agent_chat` or unattended session has no origin.
    """
    if origin is None:
        return "no_origin"
    if tool_name == "lead_intake_resolve" and (
        not origin.attended
        or not auth.chat_session_id
        or origin.chat_session_id != auth.chat_session_id
    ):
        return "no_origin"
    async with runtime.session_factory() as session:
        live = await get_live_thread_session(
            session,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            account_id=auth.account_id,
        )
    if live is None:
        return "no_live_session"
    if tool_name == "lead_intake_resolve" and (
        live.effective_config is None
        or live.effective_config.chat_session_id != auth.chat_session_id
    ):
        return "no_live_session"
    try:
        ma_session = await retrieve_session(
            runtime.client, live.ma_session_id, scope=mcp_scope(auth)
        )
        if require_public_context:
            facts = session_facts(
                ma_session.metadata,
                owned=True,
                legacy_thread_id=origin.thread_id,
            )
            # A first mention in a channel creates its session before Discord opens
            # the thread, so that session is stamped with the channel alone. The
            # live row read above is keyed on this origin's thread, so the
            # session is this thread's own.
            opened_here = facts.thread is None and facts.channel == origin.parent_channel_id
            if (
                facts.private
                or facts.channel is None
                or (
                    (facts.channel, facts.thread) != (origin.parent_channel_id, origin.thread_id)
                    and not opened_here
                )
            ):
                return "source_context_unavailable"
        if ma_session.agent.id != origin.responder_ma_agent_id:
            return "other_agents_session"
        if has_confirmation_gate(
            [tool.model_dump(mode="json") for tool in ma_session.agent.tools], tool_name=tool_name
        ) or await _recorded_as_gated(
            runtime,
            auth,
            live,
            tool_name=tool_name,
            ma_agent_id=ma_session.agent.id,
            reported_tools_sha256=hash_tools(ma_session.agent.tools),
        ):
            return None
        return "session_not_gated"
    except ScopeViolation:
        return "other_agents_session"
    except anthropic.APIError as exc:
        # Unreadable (a deleted session, an outage) is not evidence of a card.
        _log.warning(
            "session_gate.check_failed",
            tool=tool_name,
            ma_session_id=live.ma_session_id,
            error=str(exc),
        )
        return "session_unreadable"


async def _recorded_as_gated(
    runtime: McpRuntime,
    auth: AuthIdentity,
    live: ThreadSessionRow,
    *,
    tool_name: str,
    ma_agent_id: str,
    reported_tools_sha256: str,
) -> bool:
    """Whether the tools daimon recorded for `live` are gated ones, `tool_name` asking.

    Only for a session MA reports without its per-session overrides, i.e. with
    the agent's own tools (`reported_tools_sha256`): a report showing other
    tools, such as a session switched to `always_allow` out of band, is taken
    at its word. The bind records the hash of the tools it last sent the
    session; equal to the gated tools a session for this caller gets now
    (`session_tools`, asking before publishing or not), it is the server's own
    record of the card. An agent changed since that bind reads as not gated.

    Non-watch actions also recognize the pre-watch hash format, omitting only
    the newly added watch gates. The requested action must still ask in that
    exact recorded template. Watch enrollment never uses this compatibility path.
    """
    recorded = live.effective_config
    if recorded is None:
        return False
    agent = await retrieve_agent(
        runtime.client,
        ma_agent_id,
        scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
    )
    hidden = await resolve_hidden_mcp_server_names(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id),
        account_id=auth.account_id,
        server_urls={server.name: server.url for server in agent.mcp_servers},
    )
    # MA reporting the agent's own tools means it left the overrides out.
    if reported_tools_sha256 not in {
        hash_tools(agent.tools),
        hash_tools(visible_tools(agent, hidden)),
    }:
        return False
    public_url = runtime.settings.mcp.public_url
    # The bind may have been made with other rules than today's, so both the
    # current set and none are tried; neither matching reads as not gated.
    rule_tools = await daimon_rule_tools_for(
        runtime.session_factory,
        runtime.settings.tool_safety,
        tenant_id=auth.tenant_id,
        agent_id=agent.id,
    )
    selection = backend_selection_enabled(runtime.settings.turn)
    confirmed = backend_switch_tools(enabled=selection) | microsoft_send_tools(
        enabled=runtime.settings.microsoft_accounts.configured,
        tool_safety_on=runtime.settings.tool_safety.enabled,
    )
    # A bind sent while the retired chat channel switch was also gated holds
    # that one more gated tool; it still proves every card it gated.
    legacy = confirmed | ({"request_channel_backend_switch"} if selection else set[str]())
    # Pre-fleet-config sessions prove only the tools that were actually gated.
    before_config = confirmed - {
        "request_fleet_config",
        "request_provider_allowlist",
        "request_channel_pin_clear",
    }
    before_config_legacy = before_config | (
        {"request_channel_backend_switch"} if selection else set[str]()
    )
    # Sessions bound after fleet config but before the channel pin clear.
    before_pin_clear = confirmed - {"request_channel_pin_clear"}
    lead_tools = await confirmation_tools(runtime.session_factory, auth.tenant_id)
    for confirmed_tools, pre_watch in dict.fromkeys(
        (candidate | lead_tools | watch_tools, not watch_tools)
        for candidate in (confirmed, legacy, before_config, before_config_legacy, before_pin_clear)
        for watch_tools in (WATCH_CONFIRMED_TOOLS, frozenset[str]())
        if watch_tools or tool_name not in WATCH_CONFIRMED_TOOLS
    ):
        for asks_before_publishing in (False, True):
            for daimon_rule_tools in dict.fromkeys((frozenset[str](), rule_tools)):
                tools = session_tools(
                    agent,
                    hidden,
                    tool_safety=runtime.settings.tool_safety,
                    public_url=None if public_url is None else str(public_url),
                    asks_before_publishing=asks_before_publishing,
                    confirmed_tools=confirmed_tools,
                    daimon_rule_tools=daimon_rule_tools,
                    legacy_memory_servers=(
                        get_repo_memory_settings(runtime.settings).legacy_mcp_server_names
                        if get_repo_memory_settings(runtime.settings).enabled
                        else ()
                    ),
                )
                if pre_watch:
                    # Policy-on sessions add watch gates implicitly too. Rebuild
                    # the exact pre-watch template for non-watch actions only;
                    # no old hash can authorize watch enrollment or rotation.
                    tools = [
                        tool.model_copy(
                            update={
                                "configs": [
                                    config
                                    for config in tool.configs or []
                                    if config.name not in WATCH_CONFIRMED_TOOLS
                                ]
                            }
                        )
                        if tool.type == "mcp_toolset" and tool.mcp_server_name == DAIMON_SERVER_NAME
                        else tool
                        for tool in tools
                    ]
                if hash_tools(tools) == recorded.tools_sha256 and has_confirmation_gate(
                    [tool.model_dump(mode="json") for tool in tools], tool_name=tool_name
                ):
                    return True
    return False

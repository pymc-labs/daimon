"""Pure read/write classification and per-call decisions for attached tools.

An agent can reach third-party MCP servers (Linear, Notion, a CRM) whose
tools change shared records. Managed Agents runs those tools inside its own
sandbox, so the only place daimon can stand between the model and a write is
the platform's per-toolset `permission_policy`: with `always_ask`, MA pauses
the session on a `requires_action` idle before each call and waits for a
`user.tool_confirmation`. This module decides what that confirmation says.

Three questions, each a total function of its arguments:

- `classify_tool` — is this tool a read or a write? An operator override wins,
  then the tool's own MCP annotations when the caller has them (a destructive
  hint is always a write; a read-only hint alone is a read), then the verb
  the tool name starts with. Anything still unknown is a write: an unfamiliar
  tool is assumed to change something.
- `decide_tool_call` — given that class and whether a person is present,
  allow, ask the person, or deny. Reads run; writes ask in chat; writes in an
  unattended run (routine, wake, smoke) are denied unless the operator allowed
  that server or tool there; a server or tool on the deny list never runs.
- `toolset_permission_policy` — which MA policy a toolset carries, so the
  pause above happens at all. Daimon's own server keeps `always_allow`: its
  tools are daimon code with their own authorization (`operation_policy`).
  "Daimon's own" is verified, not a name match: see `trusted_servers_for`
  and `heal_reserved_server`.
  `session_tools_for_policy` applies it to a session's tool list, which
  `create_session` sends as a per-session override. Confirmed daimon tools
  still ask, including: `add_skill`, once its input names the reviewed `content_hash`
  (`CONFIRMED_DAIMON_TOOLS`), since it puts new files in front of everyone
  the agent answers. A run nobody watches never confirms one, whatever
  `unattended_writes` allows. Daimon's `PUBLISH_TOOLS` ask too in a session
  that asks before publishing (an agent with a rule, `AgentPermissions.publishes`),
  tool safety on or off.
- Agent tool rules reach daimon's own tools too (`agent_rules` on). A rule
  `daimon-mcp/<tool>` of `ask` or `deny` makes the session ask on that tool
  and on the `call_tool` proxy (`daimon_rule_tools`), and the driver reads the
  call through the proxy (`unwrap_daimon_call`) to apply the rule: `deny`
  refuses, `ask` waits for the card (refused when nobody is here), and a call
  with no rule, or an `allow` rule, runs without a card. Precedence on a
  daimon tool: the operator's deny of a confirmed write, then an agent `deny`,
  then the fixed gates (`PUBLISH_TOOLS`, `CONFIRMED_DAIMON_TOOLS`), then an
  agent `ask`. A rule never loosens a fixed gate.

With `enabled=False` (the default), ordinary toolsets stay `always_allow`.
Person-only actions, including watch creation, resume and key rotation, retain
their confirmation cards regardless of that switch.

Pure module — no I/O, no clock, no settings lookup. The shells (session
create, turn driver, adapter cards) read a policy value and a `ToolVerdict` from here
and keep their own copy and I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, cast

from daimon.core.mcp_server_url import is_session_mcp_url, same_mcp_endpoint
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "APPROVAL_TOOLS",
    "CONFIRMED_DAIMON_TOOLS",
    "WATCH_CONFIRMED_TOOLS",
    "DAIMON_SERVER_NAME",
    "OPEN_TOOL_SAFETY",
    "PUBLISH_TOOLS",
    "PERMISSION_SETTER_TOOLS",
    "SERVER_SEPARATOR",
    "PermissionPolicyType",
    "ToolAnnotations",
    "ToolCall",
    "ToolEffect",
    "ToolSafetyPolicy",
    "ToolVerdict",
    "VerdictReason",
    "backend_switch_tools",
    "get_mcp_server_name_rejection",
    "microsoft_send_tools",
    "classify_tool",
    "daimon_endpoint",
    "decide_tool_call",
    "has_confirmation_gate",
    "gate_protected_tool",
    "heal_reserved_server",
    "needs_annotations",
    "proxy_refusal",
    "unwrap_daimon_call",
    "session_tools_for_policy",
    "toolset_permission_policy",
    "trusted_servers_for",
]

#: The search transform's proxy: a chat identity lists `search_tools`,
#: `call_tool` and the `APPROVAL_TOOLS`, and runs everything else as
#: `call_tool(name=<tool>, arguments=<input>)`.
PROXY_TOOL_NAME = "call_tool"
SEARCH_TOOL_NAME = "search_tools"
#: `daimon-mcp/*`: an agent-wide `deny` on every daimon tool. Never a tool name:
#: it gates the proxy, but gets no config of its own.
WILDCARD_TOOL = "*"

ToolEffect = Literal["read", "write"]
PermissionPolicyType = Literal["always_allow", "always_ask"]
VerdictReason = Literal[
    "protected_tool_name",
    #: Enforcement is off for this deployment.
    "disabled",
    #: Not a third-party MCP tool (sandbox tools, daimon's own server).
    "not_attached",
    "read",
    #: A write while a person is present: they are asked first.
    "write_needs_confirmation",
    #: A write in a run nobody is watching, on a server or tool the operator
    #: allowed there.
    "unattended_write_allowed",
    #: A write in a run nobody is watching, not allowed there.
    "unattended_write",
    #: The operator put this server or tool on the deny list.
    "denied_by_operator",
    #: Publishing from a session that asks first, while a person is present.
    "publish_needs_confirmation",
    #: Publishing from a session that asks first, in a run nobody is watching.
    "unattended_publish",
    "agent_rule_allow",
    "agent_rule_ask",
    "agent_rule_deny",
]


class ResolvedAgentToolRule(BaseModel):
    """A stored rule after tenant, agent and endpoint binding have matched."""

    model_config = ConfigDict(frozen=True)

    rule: Literal["allow", "ask", "deny"]
    scope: Literal["chat", "chat_and_routines"] = "chat"


def stricter_agent_tool_rule(
    first: ResolvedAgentToolRule | None, second: ResolvedAgentToolRule | None
) -> ResolvedAgentToolRule | None:
    """Combine matching agent-wide and channel rules, keeping the stricter one."""
    if first is None:
        return second
    if second is None:
        return first
    rank = {"allow": 0, "ask": 1, "deny": 2}
    if rank[first.rule] != rank[second.rule]:
        return first if rank[first.rule] > rank[second.rule] else second
    if first.scope == "chat" or second.scope == "chat":
        return first.model_copy(update={"scope": "chat"})
    return first


#: Name of the deployment's own MCP server entry on every agent. Same value as
#: `daimon.core.defaults.mcp_merge.DAIMON_MCP_SERVER_NAME`; repeated here so
#: this module (and `config`, which imports it) does not pull the defaults
#: package in. A test holds the two equal.
DAIMON_SERVER_NAME: Final[str] = "daimon-mcp"

#: `unattended_writes` entry that allows every write in unattended runs.
ANY_KEY: Final[str] = "*"

#: Joins a server and a tool in `server/tool` keys. A server name never holds
#: it: attaching one is refused, so the first `/` of a key always ends the
#: server. A call on a server that holds it anyway (attached before the
#: refusal) matches no key reliably, and `decide_tool_call` never allows it.
SERVER_SEPARATOR: Final[str] = "/"

#: Daimon's own tools that wait for the card like a third-party write, keyed
#: to the input field whose presence makes the call the write. Without it the
#: call only previews, and runs. With it, an unattended run is always refused.
CONFIRMED_DAIMON_TOOLS: Final[Mapping[str, str]] = {
    "start_long_job": "command",
    "create_watch": "source",
    "resume_watch": "watch_id",
    "rotate_watch_key": "watch_id",
    "add_skill": "content_hash",
    "lead_intake_resolve": "resolution",
    "request_agent_backend_switch": "preview_digest",
    "request_fleet_config": "preview_digest",
    "request_provider_allowlist": "preview_digest",
    "request_channel_pin_clear": "preview_digest",
    # Every call names its action, so every call asks.
    "microsoft_send": "action",
    # Every call names its method; asks only with tool safety on.
    "microsoft_write": "method",
}


WATCH_CONFIRMED_TOOLS: Final[frozenset[str]] = frozenset(
    {"create_watch", "resume_watch", "rotate_watch_key", "start_long_job"}
)


def confirmation_gated_tools() -> frozenset[str]:
    """Daimon tools that only run after a person confirms on a card (or publish)."""
    return (
        frozenset(CONFIRMED_DAIMON_TOOLS)
        | frozenset(PUBLISH_TOOLS)
        | WATCH_CONFIRMED_TOOLS
        | {"lead_intake_resolve", "set_routine_sandbox_tools"}
    )


def backend_switch_tools(*, enabled: bool) -> frozenset[str]:
    """Project authoritative backend-selection eligibility into session controls."""
    return (
        frozenset(
            {
                "request_agent_backend_switch",
                "request_fleet_config",
                "request_provider_allowlist",
                "request_channel_pin_clear",
            }
        )
        if enabled
        else frozenset()
    )


def microsoft_send_tools(*, enabled: bool, tool_safety_on: bool = False) -> frozenset[str]:
    """The Microsoft tools a session asks before, where Microsoft accounts are configured.

    Mail and invites sent as the person ask on the card, tool safety on or off.
    Changes to their files and notes (`microsoft_write`) ask only with it on,
    so deployments without tool safety, or without Microsoft accounts, keep
    the session tools they had.
    """
    if not enabled:
        return frozenset()
    return frozenset({"microsoft_send", *(("microsoft_write",) if tool_safety_on else ())})


def _always_confirmed_tools() -> frozenset[str]:
    """Every other confirmation-table entry requires review when policy is on."""
    return (
        frozenset(CONFIRMED_DAIMON_TOOLS)
        - frozenset({"lead_intake_resolve"})
        - backend_switch_tools(enabled=True)
        - microsoft_send_tools(enabled=True, tool_safety_on=True)
    )


#: Daimon's own tools that put content behind a link whoever holds it opens.
#: A session that asks before publishing waits for the card on each, and an
#: unattended run is refused them.
PUBLISH_TOOLS: Final[frozenset[str]] = frozenset(
    {"publish_report", "create_notebook_upload_url", "create_attachment_upload_url"}
)

#: Daimon's tools a session can hold on `always_ask` by their own name.
APPROVAL_TOOLS: Final[frozenset[str]] = frozenset(
    {*CONFIRMED_DAIMON_TOOLS, *PUBLISH_TOOLS, "set_routine_sandbox_tools"}
)

# Permission changes cannot be delegated by switching off write confirmation.
# These names are protected even on unrecognised hosts or unrelated servers.
PERMISSION_SETTER_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "set_approval_gate",
        "set_channel_rule",
        "set_agent_rule",
        "set_agent_tool_rule",
        "clear_agent_tool_rule",
        "set_channel_agent_tool_rule",
        "clear_channel_agent_tool_rule",
        "set_routine_sandbox_tools",
        "set_channel_environment",
        "clear_channel_environment",
        "set_channel_skills",
        "add_channel_skill",
        "remove_channel_skill",
        "set_channel_budget",
        "clear_channel_budget",
        "set_agent_backend",
        "set_agent_default",
        "clear_agent_default",
        "set_thread_participation",
        "set_plain_text_mentions",
        "grant_admin",
        "revoke_admin",
        "set_access_policy",
        "clear_access_policy",
    }
)


def gate_protected_tool(tool_name: str) -> bool:
    """Names that never receive automatic authority from a gate override.

    Endpoint identity is additional protection, not proof that another host
    serves a third party. The proxy is protected so it cannot wrap a setter.
    """
    return (
        tool_name in CONFIRMED_DAIMON_TOOLS
        or tool_name in PUBLISH_TOOLS
        or tool_name in PERMISSION_SETTER_TOOLS
        or tool_name == PROXY_TOOL_NAME
        or (
            tool_name.startswith(("set_", "clear_", "grant_", "revoke_", "configure_"))
            and any(
                part in tool_name
                for part in ("permission", "approval", "policy", "rule", "admin", "gate")
            )
        )
    )


# Leading verbs that name a read. Deliberately short: a verb that is sometimes
# a write ("run_", "sync_", "export_", and "query_", which on a SQL server can
# be anything) stays out, so it falls to the fail-closed `write` default.
_READ_VERBS: Final[tuple[str, ...]] = (
    "get",
    "list",
    "search",
    "read",
    "fetch",
    "find",
    "describe",
    "view",
    "show",
    "count",
    "lookup",
    "retrieve",
    "check",
    "preview",
    "download",
    # A noun, but in a tool name it usually names a report to read
    # ("eap/eap_briefing"). A heuristic like every word here: a briefing tool
    # that also sends or publishes says so ("send_briefing", "briefing_publish")
    # and stays a write through `_WRITE_WORDS`.
    "briefing",
)

# Whole tool names that read although the verb comes last. GitHub's MCP server
# names its read tools "<object>_read" and picks the action in a `method`
# argument (get, get_diff, list ...); each carries `readOnlyHint: true` in the
# server's tool snapshots. A trailing "read" is not a rule: the same server's
# "mark_all_notifications_read" writes. So this is an exact list, not a suffix.
_READ_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "issue_read",
        "pull_request_read",
    }
)

# Verbs that read only when the name also says what comes back. A bare "ask"
# is an action (daimon's own agent_chat `ask` starts a turn in another agent),
# and "verify_email" can send a code, so these two fail closed unless the name
# is a question-and-answer shape: "ask_wiki_question" (DeepWiki),
# "eap_verify_answer". The match is still only a name heuristic; a server that
# misnames a write is caught by an operator `effects` pin, not by this list.
_QUESTION_VERBS: Final[Mapping[str, frozenset[str]]] = {
    "ask": frozenset({"question"}),
    "verify": frozenset({"answer"}),
}

# Words that make a name a write wherever they appear: a write verb anywhere
# ("get_or_create", "fetch_and_delete", "list_delete") or a conjunction that
# chains a second action onto a read ("search_and_replace").
_WRITE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "add",
        "append",
        "approve",
        "archive",
        "assign",
        "cancel",
        "clear",
        "close",
        "create",
        "delete",
        "destroy",
        "drop",
        "edit",
        "grant",
        "insert",
        "invite",
        "kill",
        "merge",
        "modify",
        "move",
        "patch",
        "post",
        "publish",
        "purge",
        "put",
        "remove",
        "rename",
        "replace",
        "reset",
        "revoke",
        "send",
        "set",
        "share",
        "update",
        "upload",
        "upsert",
        "write",
        # Chaining words: a read that goes on to do something else.
        "and",
        "or",
        "then",
    }
)

# Prefix words that are themselves actions, so a read verb after them is the
# object of the action, not the tool's verb ("run_query", "call_get_page").
_ACTION_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "apply",
        "call",
        "do",
        "exec",
        "execute",
        "export",
        "import",
        "invoke",
        "perform",
        "run",
        "submit",
        "sync",
        "trigger",
    }
)

_WORD_SPLIT: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class ToolAnnotations(BaseModel):
    """The MCP tool hints this module reads, when a caller has them.

    MA does not forward a third-party server's annotations to daimon, so the
    approval deciders read them from the server's own `tools/list`
    (`daimon.core.mcp_tool_annotations`), only for a server listed in
    `ToolSafetyPolicy.trusted_annotations`. For any other server, or when that
    read fails, or the tool carries no hints, these are absent and the name
    decides.
    """

    model_config = ConfigDict(frozen=True)

    read_only_hint: bool | None = None
    destructive_hint: bool | None = None


class ToolSafetyPolicy(BaseModel):
    """What the operator decided about attached tools.

    Keys name a server (`linear`) or one tool on it (`linear/create_issue`);
    a tool key is more specific than its server key and wins.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Classify attached (third-party) MCP tools as read or write and gate the writes: "
            "a write in chat waits for the requester to press Approve on a confirmation card, "
            "and a write in a routine or other unattended run is refused unless listed in "
            "unattended_writes. A new session gets it when created; an existing chat session "
            "gets it at its next bind, and an unattended turn (agent chat, daimon run, a "
            "routine, a checkpoint) sets it on the session before each send. Off lets "
            "ordinary third-party calls run, but Daimon's card-only actions still ask "
            "(lead resolution, a backend switch and Microsoft mail and invites; file changes only "
            "with it on)."
        ),
    )
    agent_rules: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
        description=(
            "Apply workspace-admin tool rules for individual agents and MCP tools: third-party "
            "tools, and Daimon's own daimon-mcp/<tool> (ask and deny). "
            "Off by default; stored rules remain editable but inactive until enabled."
        ),
    )
    allow_gate_overrides: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
        description=(
            "Let native server admins change third-party MCP write approvals from /here "
            "at server, channel or thread scope. The most specific explicit on/off setting wins. "
            "Stored settings are ignored while off. Fixed confirmation and permission-setter "
            "names never receive an override, regardless of server host. Deny rules, direct "
            "messages and unattended_writes retain their existing behavior."
        ),
    )
    effects: dict[str, ToolEffect] = Field(
        default_factory=dict,
        description=(
            'Override the read/write class, e.g. {"linear/get_team": "read", "notion": '
            '"write"}. Keys are a server name or server/tool. Unlisted tools are classified '
            "from the server's own MCP annotations when the server is listed in "
            "trusted_annotations and sends them (readOnlyHint true is a read unless "
            "destructiveHint is also true), otherwise from their name: a plain get_, list_, "
            "search_ ... is a read; a compound name (get_or_create, search_and_replace, "
            "run_query) and anything unknown is a write."
        ),
    )
    trusted_annotations: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        description=(
            'MCP server names whose own tool annotations count, e.g. ["bridge"]. For a listed '
            "server Daimon lists its tools once per turn and runs a tool marked readOnlyHint "
            "true (without destructiveHint true) as a read, and one marked destructiveHint "
            "true or readOnlyHint false as a write; each such auto-allow is logged. An "
            "effects pin or a denied entry still wins. An unlisted server's annotations are "
            "never read, so its tools are classified by name. Empty (the default) reads none. "
            "Trust is by server name across every agent in the deployment: any agent that "
            "attaches a server with a listed name has its hints counted, so list only names "
            "you control. Names are matched exactly; there is no '*' wildcard."
        ),
    )
    denied: tuple[str, ...] = Field(
        default=(),
        description=(
            'Servers or server/tool pairs that are always refused, e.g. ["hubspot/delete_deal"].'
        ),
    )
    unattended_writes: tuple[str, ...] = Field(
        default=(),
        description=(
            "Servers or server/tool pairs whose writes may run in routines and other "
            'unattended runs, e.g. ["linear/create_issue"]. "*" allows every write there. '
            "Daimon's own add_skill never confirms unattended."
        ),
    )
    confirmation_timeout_s: int = Field(
        default=600,
        ge=30,
        le=86400,
        description=(
            "How long an approval card waits for the requester's Approve or Deny, in "
            "seconds (up to 24 hours), before it expires and the call is refused. A card "
            "never outlives its turn: it shows and keeps the earlier of this and the "
            "turn's own deadline. Production keeps the 10-minute default; staging can set "
            "it short (60-90) so a precheck or load test does not hold a turn for ten "
            "minutes."
        ),
    )


OPEN_TOOL_SAFETY: Final[ToolSafetyPolicy] = ToolSafetyPolicy()


def get_mcp_server_name_rejection(server_name: str) -> str | None:
    """Return the rejection reason when ``server_name`` holds ``/``, else None.

    Tool keys are ``server/tool``, so a server named ``linear/bridge`` would
    stand for tools on ``linear``: its rules, operator keys and unattended
    allowlists could reach the wrong server. Pure; the caller surfaces it.
    """
    if SERVER_SEPARATOR in server_name:
        return (
            f"Connection names cannot contain '{SERVER_SEPARATOR}' ('{server_name}'): it "
            "separates a server from a tool in tool keys. Pick a different server name."
        )
    return None


class ToolCall(BaseModel):
    """One blocked tool call, as the driver sees it on a `requires_action` idle.

    `server_name` is `None` for sandbox and custom tools, which carry no MCP
    server.
    """

    model_config = ConfigDict(frozen=True)

    tool_use_id: str
    server_name: str | None
    tool_name: str
    input: dict[str, object] = Field(default_factory=dict)
    permission: Literal["allow", "ask", "deny"] | None = None

    @property
    def key(self) -> str:
        """`server/tool`, the form operator keys use."""
        return f"{self.server_name}/{self.tool_name}"


class ToolVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["allow", "ask", "deny"]
    effect: ToolEffect
    reason: VerdictReason


def _keyed(keys: dict[str, ToolEffect], *, server_name: str, tool_name: str) -> ToolEffect | None:
    tool_key = f"{server_name}/{tool_name}"
    if tool_key in keys:
        return keys[tool_key]
    return keys.get(server_name)


def _listed(keys: tuple[str, ...], *, server_name: str, tool_name: str) -> bool:
    return f"{server_name}/{tool_name}" in keys or server_name in keys


def _name_says_read(tool_name: str) -> bool:
    """True only for a name that plainly reads: its leading verb is a read
    verb, and nothing else in it writes or chains another action.

    The leading verb is the first word, or the second after a prefix that is
    not itself an action (a server prefixing its own name, "notion_get_page").
    Everything else — compound names, action prefixes, unknown verbs — is a
    write.
    """
    spaced = _CAMEL_BOUNDARY.sub("_", tool_name).lower()
    words = [word for word in _WORD_SPLIT.split(spaced) if word]
    if not words or any(word in _WRITE_WORDS for word in words):
        return False
    if words[0] in _ACTION_PREFIXES:
        return False
    if "_".join(words) in _READ_TOOL_NAMES:
        return True
    for slot in (0, 1)[: len(words)]:
        verb = words[slot]
        if verb in _READ_VERBS:
            return True
        objects = _QUESTION_VERBS.get(verb)
        if objects is not None:
            return any(word in objects for word in words[slot + 1 :])
    return False


def classify_tool(
    policy: ToolSafetyPolicy,
    *,
    server_name: str,
    tool_name: str,
    annotations: ToolAnnotations | None = None,
) -> ToolEffect:
    """Return whether `server_name`'s `tool_name` reads or writes.

    Order: operator override (tool key, then server key); `destructive_hint`
    true is a write even beside `read_only_hint` true; `read_only_hint` (an
    explicit False is a write whatever the name says); a plainly-read name
    (`_name_says_read`); otherwise `write`. So `destructive_hint=True` or
    `read_only_hint=False` makes a write, only `read_only_hint=True` without a
    destructive hint makes a read, and a missing hint leaves the name to decide.
    """
    override = _keyed(policy.effects, server_name=server_name, tool_name=tool_name)
    if override is not None:
        return override
    if annotations is not None:
        if annotations.destructive_hint is True or annotations.read_only_hint is False:
            return "write"
        if annotations.read_only_hint is True:
            return "read"
    return "read" if _name_says_read(tool_name) else "write"


def _gated(server_name: str | None, trusted_servers: frozenset[str]) -> bool:
    return server_name is not None and server_name not in trusted_servers


def needs_annotations(
    policy: ToolSafetyPolicy, call: ToolCall, *, trusted_servers: frozenset[str] = frozenset()
) -> bool:
    """Whether `decide_tool_call` would read `call`'s annotations: policy on, a
    gated server the operator trusts for annotations (`trusted_annotations`),
    and no operator `effects` override or `denied` entry for it.

    Mirrors the guards in `decide_tool_call` before its `classify_tool` call
    (`policy.enabled`, a server, `_gated`) and the override check at the top of
    `classify_tool`; a change to either must change this too, or a decider
    skips a read that would have mattered (the name then decides, which only
    ever asks more).
    """
    return (
        policy.enabled
        and call.server_name is not None
        and _gated(call.server_name, trusted_servers)
        and call.server_name in policy.trusted_annotations
        and _keyed(policy.effects, server_name=call.server_name, tool_name=call.tool_name) is None
        and not _listed(policy.denied, server_name=call.server_name, tool_name=call.tool_name)
    )


def _confirmed_daimon_write(
    call: ToolCall,
    *,
    trusted_servers: frozenset[str],
    confirmed_tools: frozenset[str] = frozenset(),
) -> bool:
    field = (
        CONFIRMED_DAIMON_TOOLS.get(call.tool_name)
        if call.tool_name in _always_confirmed_tools() or call.tool_name in confirmed_tools
        else None
    )
    # Keep the reserved-name unattended refusal even without a binding. Aliases
    # require endpoint identity; the name alone never grants an exemption.
    return (
        (call.server_name == DAIMON_SERVER_NAME or call.server_name in trusted_servers)
        and field is not None
        and call.input.get(field) is not None
    )


def is_publish_call(call: ToolCall, trusted_servers: frozenset[str]) -> bool:
    """`call` publishes through daimon's own server (`PUBLISH_TOOLS`)."""
    return call.server_name in trusted_servers and call.tool_name in PUBLISH_TOOLS


def proxy_refusal(name: str) -> str | None:
    """Why `call_tool` may not carry `name`, or `None` when it may.

    The tools that wait for a person's Approve are listed under their own name
    so Managed Agents pauses on the name it sees; through the proxy the pause
    would be on `call_tool`, and a card there would approve nothing. The MCP
    server refuses them with this text, and so does the driver.
    """
    if name not in APPROVAL_TOOLS:
        return None
    return (
        f"Call {name} directly, not through call_tool: it is listed under its own "
        "name so the person can approve it. Nothing was done."
    )


def unwrap_daimon_call(call: ToolCall, trusted_servers: frozenset[str]) -> ToolCall:
    """The tool a daimon `call_tool` proxy call runs, as a call of its own.

    Managed Agents pauses and reports the proxy by its own name; the rule key,
    the operator keys and the card are about the tool inside. A call on any
    other server, a direct daimon call, or a proxy call whose `name` is not a
    plain tool name comes back unchanged (the last is then looked up, and
    refused or allowed, as `call_tool` itself).
    """
    if call.server_name not in trusted_servers or call.tool_name != PROXY_TOOL_NAME:
        return call
    inner = call.input.get("name")
    if (
        not isinstance(inner, str)
        or not inner
        or "/" in inner
        or inner in {PROXY_TOOL_NAME, SEARCH_TOOL_NAME}
    ):
        return call
    arguments = call.input.get("arguments")
    forwarded = cast("dict[str, object]", arguments) if isinstance(arguments, dict) else {}
    return call.model_copy(update={"tool_name": inner, "input": dict(forwarded)})


def daimon_endpoint(public_url: str | None) -> dict[str, str]:
    """The deployment's own endpoint under the reserved name, for rule lookups.

    The session talks to this address whatever URL the agent's `daimon-mcp`
    entry names (`heal_reserved_server`), so a rule bound to it is about the
    server that really ran. Empty without a `public_url`.
    """
    return {DAIMON_SERVER_NAME: public_url} if public_url else {}


def trusted_servers_for(
    public_url: str | None, server_urls: Mapping[str, str] | None = None
) -> frozenset[str]:
    """Own-server identities in the effective session endpoint bindings.

    The reserved name is healed before session creation. Legacy aliases of
    the same endpoint must retain every fixed Daimon confirmation as well.
    This exclusion comparison never grants credentials to an alias.
    """
    if not public_url:
        return frozenset()
    return frozenset({DAIMON_SERVER_NAME}) | frozenset(
        name for name, url in (server_urls or {}).items() if same_mcp_endpoint(url, public_url)
    )


def heal_reserved_server(
    policy: ToolSafetyPolicy,
    servers: Sequence[Mapping[str, Any]],
    *,
    public_url: str | None,
    confirmed_tools: frozenset[str] = frozenset(),
) -> list[dict[str, Any]] | None:
    """`servers` with a foreign-URL `daimon-mcp` entry re-pointed, or `None`.

    Only while the policy is on and a `public_url` exists (the exemption in
    `trusted_servers_for` depends on it). A `daimon-mcp` entry naming another
    URL is the reserved name worn by a server daimon does not run; the session
    gets the real endpoint under that name instead, so the exemption can never
    cover a third party.
    """
    if (not policy.enabled and not confirmed_tools) or not public_url:
        return None
    canonical = public_url.rstrip("/")
    changed = False
    out: list[dict[str, Any]] = []
    for server in servers:
        entry = dict(server)
        url = entry.get("url")
        if (
            entry.get("name") == DAIMON_SERVER_NAME
            and (url.rstrip("/") if isinstance(url, str) else None) != canonical
            and not (isinstance(url, str) and is_session_mcp_url(url, public_url))
        ):
            entry["url"] = public_url
            changed = True
        out.append(entry)
    return out if changed else None


def decide_tool_call(
    policy: ToolSafetyPolicy,
    call: ToolCall,
    *,
    attended: bool,
    trusted_servers: frozenset[str] = frozenset(),
    annotations: ToolAnnotations | None = None,
    confirmed_tools: frozenset[str] = frozenset(),
    agent_rule: ResolvedAgentToolRule | None = None,
) -> ToolVerdict:
    """Provider permission can tighten a host verdict, never grant consent."""
    verdict = _decide_tool_call(
        policy,
        call,
        attended=attended,
        trusted_servers=trusted_servers,
        annotations=annotations,
        confirmed_tools=confirmed_tools,
        agent_rule=agent_rule,
    )
    if call.permission == "deny":
        return ToolVerdict(outcome="deny", effect=verdict.effect, reason="agent_rule_deny")
    if call.permission == "ask" and verdict.outcome == "allow":
        return ToolVerdict(
            outcome="ask" if attended else "deny", effect=verdict.effect, reason="agent_rule_ask"
        )
    return verdict


def _decide_tool_call(
    policy: ToolSafetyPolicy,
    call: ToolCall,
    *,
    attended: bool,
    trusted_servers: frozenset[str] = frozenset(),
    annotations: ToolAnnotations | None = None,
    confirmed_tools: frozenset[str] = frozenset(),
    agent_rule: ResolvedAgentToolRule | None = None,
) -> ToolVerdict:
    """Decide one blocked call.

    `attended` is whether a person started this turn and can answer a card
    (chat) or not (routines, wakes, smoke runs). `trusted_servers` is
    `trusted_servers_for(public_url, effective_server_urls)`; empty (the default)
    gates every server.
    A trusted server's call is a write only when `CONFIRMED_DAIMON_TOOLS` says so.
    A trusted `PUBLISH_TOOLS` call pauses only in a session that asks before
    publishing, so it asks (or, unattended, is refused) even with the policy off.

    `call` is the tool itself: a daimon `call_tool` proxy call is unwrapped
    first (`unwrap_daimon_call`). With `agent_rules` on, an `agent_rule` on a
    trusted call is read like a third party's, except that it can only tighten
    what daimon already enforces: `deny` wins over the fixed gates (not over an
    operator deny of a confirmed write), `ask` pauses a tool that would
    otherwise run, and `allow`, or no rule, leaves the fixed gates as they are.
    """
    trusted = call.server_name is not None and call.server_name in trusted_servers
    trusted_rule = agent_rule if trusted and policy.enabled and policy.agent_rules else None
    if trusted_rule is not None and trusted_rule.rule == "deny":
        confirmed = _confirmed_daimon_write(
            call, trusted_servers=trusted_servers, confirmed_tools=confirmed_tools
        )
        assert call.server_name is not None
        if confirmed and _listed(
            policy.denied, server_name=call.server_name, tool_name=call.tool_name
        ):
            return ToolVerdict(outcome="deny", effect="write", reason="denied_by_operator")
        return ToolVerdict(
            outcome="deny",
            effect="write"
            if confirmed or is_publish_call(call, trusted_servers)
            else classify_tool(policy, server_name=call.server_name, tool_name=call.tool_name),
            reason="agent_rule_deny",
        )
    if is_publish_call(call, trusted_servers):
        if attended:
            return ToolVerdict(outcome="ask", effect="write", reason="publish_needs_confirmation")
        return ToolVerdict(outcome="deny", effect="write", reason="unattended_publish")
    if (
        not policy.enabled
        and call.server_name in trusted_servers
        and call.tool_name in confirmed_tools
        and _confirmed_daimon_write(
            call, trusted_servers=trusted_servers, confirmed_tools=confirmed_tools
        )
    ):
        return ToolVerdict(
            outcome="ask" if attended else "deny",
            effect="write",
            reason="write_needs_confirmation" if attended else "unattended_write",
        )
    if not policy.enabled:
        return ToolVerdict(outcome="allow", effect="read", reason="disabled")
    if call.server_name is None:
        return ToolVerdict(outcome="allow", effect="read", reason="not_attached")
    if attended and call.server_name not in trusted_servers and gate_protected_tool(call.tool_name):
        # Endpoint comparison cannot prove another hostname is third-party.
        # Protected names cannot be widened by annotations, operator read hints,
        # an agent allow rule, or a scoped approval switch. Denials still win.
        if _listed(policy.denied, server_name=call.server_name, tool_name=call.tool_name):
            return ToolVerdict(outcome="deny", effect="write", reason="denied_by_operator")
        if policy.agent_rules and agent_rule is not None and agent_rule.rule == "deny":
            return ToolVerdict(outcome="deny", effect="write", reason="agent_rule_deny")
        return ToolVerdict(outcome="ask", effect="write", reason="protected_tool_name")
    if SERVER_SEPARATOR in call.server_name and _gated(call.server_name, trusted_servers):
        # Its keys alias another server's tools, so neither an operator
        # override nor a rule can be trusted to mean it: always a write.
        if _listed(policy.denied, server_name=call.server_name, tool_name=call.tool_name):
            return ToolVerdict(outcome="deny", effect="write", reason="denied_by_operator")
        return ToolVerdict(
            outcome="ask" if attended else "deny",
            effect="write",
            reason="write_needs_confirmation" if attended else "unattended_write",
        )
    effect: ToolEffect
    if _gated(call.server_name, trusted_servers):
        effect = classify_tool(
            policy, server_name=call.server_name, tool_name=call.tool_name, annotations=annotations
        )
    elif _confirmed_daimon_write(
        call, trusted_servers=trusted_servers, confirmed_tools=confirmed_tools
    ):
        effect = "write"
    elif trusted_rule is not None and trusted_rule.rule == "ask":
        return ToolVerdict(
            outcome="ask" if attended else "deny",
            effect=classify_tool(policy, server_name=call.server_name, tool_name=call.tool_name),
            reason="agent_rule_ask",
        )
    elif trusted_rule is not None:
        return ToolVerdict(outcome="allow", effect="read", reason="agent_rule_allow")
    else:
        return ToolVerdict(outcome="allow", effect="read", reason="not_attached")
    if _listed(policy.denied, server_name=call.server_name, tool_name=call.tool_name):
        return ToolVerdict(outcome="deny", effect=effect, reason="denied_by_operator")
    if policy.agent_rules and _gated(call.server_name, trusted_servers) and agent_rule is not None:
        if agent_rule.rule == "deny":
            return ToolVerdict(outcome="deny", effect=effect, reason="agent_rule_deny")
        if agent_rule.rule == "ask":
            return ToolVerdict(
                outcome="ask" if attended else "deny", effect=effect, reason="agent_rule_ask"
            )
        if attended or agent_rule.scope == "chat_and_routines":
            return ToolVerdict(outcome="allow", effect=effect, reason="agent_rule_allow")
    if effect == "read":
        return ToolVerdict(outcome="allow", effect=effect, reason="read")
    if attended:
        return ToolVerdict(outcome="ask", effect=effect, reason="write_needs_confirmation")
    if not _confirmed_daimon_write(
        call, trusted_servers=trusted_servers, confirmed_tools=confirmed_tools
    ) and (
        ANY_KEY in policy.unattended_writes
        or _listed(policy.unattended_writes, server_name=call.server_name, tool_name=call.tool_name)
    ):
        return ToolVerdict(outcome="allow", effect=effect, reason="unattended_write_allowed")
    return ToolVerdict(outcome="deny", effect=effect, reason="unattended_write")


def toolset_permission_policy(
    policy: ToolSafetyPolicy,
    *,
    server_name: str | None,
    trusted_servers: frozenset[str] = frozenset(),
) -> dict[str, PermissionPolicyType]:
    """The MA `permission_policy` for a toolset: `always_ask` iff gated.

    `server_name=None` is the sandbox `agent_toolset_20260401`, which always
    runs: bash and file edits inside the session's own sandbox are not
    third-party writes.
    """
    if policy.enabled and _gated(server_name, trusted_servers):
        return {"type": "always_ask"}
    return {"type": "always_allow"}


def session_tools_for_policy(
    policy: ToolSafetyPolicy,
    tools: Sequence[Mapping[str, Any]],
    *,
    trusted_servers: frozenset[str] = frozenset(),
    asks_before_publishing: bool = False,
    confirmed_tools: frozenset[str] = frozenset(),
    daimon_rule_tools: frozenset[str] = frozenset(),
) -> list[dict[str, Any]] | None:
    """`tools` (SDK params dicts) with every toolset's policy set, or `None`.

    `None` means nothing needs to change: enforcement is off and nothing asks
    before publishing, or every toolset already carries the policy wanted.
    Otherwise each gated toolset gets `always_ask` as its default and on every
    per-tool config under it, since a per-tool `always_allow` would let that
    one tool skip the pause. Daimon's own trusted toolset stays `always_allow`
    except for its `CONFIRMED_DAIMON_TOOLS`, which ask, and with
    `asks_before_publishing` its `PUBLISH_TOOLS`; with the policy off only
    publish and explicitly selected confirmed-tool configs are written.
    With the policy and `agent_rules` on, `daimon_rule_tools` (the daimon tools
    an agent rule asks or denies on, any channel) ask as well, and so does the
    `call_tool` proxy that reaches most of them; `search_tools` never asks. The
    wildcard `*` (an agent-wide deny of every daimon tool) gates the proxy only. An
    agent with no such rule gets exactly the tools it got before.
    Every config written here is complete
    (`enabled` and `permission_policy`), so the session reports back exactly
    what was sent and its tools hash the same on the next bind.

    The session carries this, not the agent: `create_session` sends it as an
    `agent_with_overrides`, so however the agent was written (panel, chat
    tools, a fork, the API directly) its sessions are gated.
    """
    if not policy.enabled and not asks_before_publishing and not confirmed_tools:
        return None
    rule_asked = (
        {PROXY_TOOL_NAME, *daimon_rule_tools} - {SEARCH_TOOL_NAME, WILDCARD_TOOL}
        if daimon_rule_tools and policy.enabled and policy.agent_rules
        else set[str]()
    )
    asked = (
        *(sorted(_always_confirmed_tools()) if policy.enabled else ()),
        *sorted(confirmed_tools),
        *(sorted(PUBLISH_TOOLS) if asks_before_publishing else ()),
        *sorted(rule_asked),
    )
    changed = False
    out: list[dict[str, Any]] = []
    for tool in tools:
        entry = dict(tool)
        server_name = entry.get("mcp_server_name")
        if entry.get("type") != "mcp_toolset":
            out.append(entry)
            continue
        default_config = dict(entry.get("default_config") or {})
        if policy.enabled:
            wanted = toolset_permission_policy(
                policy,
                server_name=server_name if isinstance(server_name, str) else "",
                # Preserve the pause on legacy aliases: their name-bound agent
                # rules are not covered by the reserved server's rule index.
                trusted_servers=trusted_servers & {DAIMON_SERVER_NAME},
            )
            if default_config.get("permission_policy") != wanted:
                default_config["permission_policy"] = wanted
                changed = True
            entry["default_config"] = default_config
            if wanted["type"] == "always_ask" and entry.get("configs"):
                configs: list[dict[str, Any]] = []
                for config in entry["configs"]:
                    changed = changed or config.get("permission_policy") != wanted
                    configs.append({**config, "permission_policy": wanted})
                entry["configs"] = configs
        if server_name in trusted_servers:
            entry["configs"], asking = _ask_for(
                entry.get("configs") or [], asked, enabled=default_config.get("enabled", True)
            )
            changed = changed or asking
        out.append(entry)
    return out if changed else None


def _ask_for(
    configs: Sequence[Mapping[str, Any]], names: Sequence[str], *, enabled: bool
) -> tuple[list[dict[str, Any]], bool]:
    """The trusted toolset's per-tool configs with each of `names` on `always_ask`.

    A config added here takes the toolset's own `enabled`, as an omitted one would.
    """
    ask: dict[str, PermissionPolicyType] = {"type": "always_ask"}
    out = [dict(config) for config in configs]
    changed = False
    for name in names:
        config = next((c for c in out if c.get("name") == name), None)
        if config is None:
            out.append({"name": name, "enabled": enabled, "permission_policy": ask})
            changed = True
        elif config.get("permission_policy") != ask:
            config["permission_policy"] = ask
            changed = True
    return out, changed


def has_confirmation_gate(tools: Sequence[Mapping[str, Any]], *, tool_name: str) -> bool:
    """Whether a session's tool list makes daimon's `tool_name` wait for a person.

    `tools` is the session's own frozen agent (`sessions.retrieve`), dumped:
    the per-tool `permission_policy` wins, else the toolset's default. A
    session created before the gate, or without tool safety, answers False.
    """
    for entry in tools:
        if entry.get("type") != "mcp_toolset" or entry.get("mcp_server_name") != DAIMON_SERVER_NAME:
            continue
        configs: Sequence[Mapping[str, Any]] = entry.get("configs") or []
        config = next((c for c in configs if c.get("name") == tool_name), None)
        default: Mapping[str, Any] = entry.get("default_config") or {}
        policy: Mapping[str, Any] = (
            (config or {}).get("permission_policy") or default.get("permission_policy") or {}
        )
        return policy.get("type") == "always_ask"
    return False

"""Pure read/write classification and per-call decisions for attached tools.

An agent can reach third-party MCP servers (Linear, Notion, a CRM) whose
tools change shared records. Managed Agents runs those tools inside its own
sandbox, so the only place daimon can stand between the model and a write is
the platform's per-toolset `permission_policy`: with `always_ask`, MA pauses
the session on a `requires_action` idle before each call and waits for a
`user.tool_confirmation`. This module decides what that confirmation says.

Three questions, each a total function of its arguments:

- `classify_tool` — is this tool a read or a write? An operator override wins,
  then the tool's own MCP annotations when the caller has them, then the verb
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
  `create_session` sends as a per-session override. One daimon tool still
  asks: `add_skill`, once its input names the reviewed `content_hash`
  (`CONFIRMED_DAIMON_TOOLS`), since it puts new files in front of everyone
  the agent answers. A run nobody watches never confirms one, whatever
  `unattended_writes` allows.

With `enabled=False` (the default) nothing changes: every toolset stays
`always_allow` and every call is allowed, which is the behaviour before this
module existed.

Pure module — no I/O, no clock, no settings lookup. The shells (session
create, turn driver, adapter cards) read a policy value and a `ToolVerdict` from here
and keep their own copy and I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CONFIRMED_DAIMON_TOOLS",
    "DAIMON_SERVER_NAME",
    "OPEN_TOOL_SAFETY",
    "PermissionPolicyType",
    "ToolAnnotations",
    "ToolCall",
    "ToolEffect",
    "ToolSafetyPolicy",
    "ToolVerdict",
    "VerdictReason",
    "classify_tool",
    "decide_tool_call",
    "heal_reserved_server",
    "session_tools_for_policy",
    "toolset_permission_policy",
    "trusted_servers_for",
]

ToolEffect = Literal["read", "write"]
PermissionPolicyType = Literal["always_allow", "always_ask"]
VerdictReason = Literal[
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
]

#: Name of the deployment's own MCP server entry on every agent. Same value as
#: `daimon.core.defaults.mcp_merge.DAIMON_MCP_SERVER_NAME`; repeated here so
#: this module (and `config`, which imports it) does not pull the defaults
#: package in. A test holds the two equal.
DAIMON_SERVER_NAME: Final[str] = "daimon-mcp"

#: `unattended_writes` entry that allows every write in unattended runs.
ANY_KEY: Final[str] = "*"

#: Daimon's own tools that wait for the card like a third-party write, keyed
#: to the input field whose presence makes the call the write. Without it the
#: call only previews, and runs. With it, an unattended run is always refused.
CONFIRMED_DAIMON_TOOLS: Final[Mapping[str, str]] = {"add_skill": "content_hash"}

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
)

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

    MA does not forward a third-party server's annotations to daimon, so for
    attached tools these are usually absent and the name decides. Callers that
    do hold them (a plugin describing its own tools, daimon's own FastMCP
    tools) pass them and they win over the name.
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
            "unattended_writes. Applies to sessions created after it is set; a chat thread "
            "keeps its current session until that is replaced. Off keeps every tool "
            "auto-approved."
        ),
    )
    effects: dict[str, ToolEffect] = Field(
        default_factory=dict,
        description=(
            'Override the read/write class, e.g. {"linear/get_team": "read", "notion": '
            '"write"}. Keys are a server name or server/tool. Unlisted tools are classified '
            "from their name: a plain get_, list_, search_ ... is a read; a compound name "
            "(get_or_create, search_and_replace, run_query) and anything unknown is a write."
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


OPEN_TOOL_SAFETY: Final[ToolSafetyPolicy] = ToolSafetyPolicy()


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
    if words[0] in _READ_VERBS:
        return True
    if words[0] in _ACTION_PREFIXES:
        return False
    return len(words) > 1 and words[1] in _READ_VERBS


def classify_tool(
    policy: ToolSafetyPolicy,
    *,
    server_name: str,
    tool_name: str,
    annotations: ToolAnnotations | None = None,
) -> ToolEffect:
    """Return whether `server_name`'s `tool_name` reads or writes.

    Order: operator override (tool key, then server key); `read_only_hint`;
    `destructive_hint`; a plainly-read name (`_name_says_read`); otherwise
    `write`.
    """
    override = _keyed(policy.effects, server_name=server_name, tool_name=tool_name)
    if override is not None:
        return override
    if annotations is not None:
        if annotations.read_only_hint is True:
            return "read"
        if annotations.destructive_hint is True:
            return "write"
    return "read" if _name_says_read(tool_name) else "write"


def _gated(server_name: str | None, trusted_servers: frozenset[str]) -> bool:
    return server_name is not None and server_name not in trusted_servers


def _confirmed_daimon_write(call: ToolCall) -> bool:
    field = CONFIRMED_DAIMON_TOOLS.get(call.tool_name)
    return (
        call.server_name == DAIMON_SERVER_NAME
        and field is not None
        and call.input.get(field) is not None
    )


def trusted_servers_for(public_url: str | None) -> frozenset[str]:
    """The servers exempt from gating in a session: the built-in daimon server,
    and only when this deployment runs one (`public_url` set).

    The exemption is by verified identity, not by name alone: with the policy
    on, `create_session` re-points a `daimon-mcp` entry that names any other
    URL at `public_url` (`heal_reserved_server`) before the session exists, so
    in every session this set is used for, `daimon-mcp` IS the deployment's
    own endpoint. Without a `public_url` there is no built-in server, and an
    entry carrying the reserved name is gated like any third party.
    """
    return frozenset({DAIMON_SERVER_NAME}) if public_url else frozenset()


def heal_reserved_server(
    policy: ToolSafetyPolicy, servers: Sequence[Mapping[str, Any]], *, public_url: str | None
) -> list[dict[str, Any]] | None:
    """`servers` with a foreign-URL `daimon-mcp` entry re-pointed, or `None`.

    Only while the policy is on and a `public_url` exists (the exemption in
    `trusted_servers_for` depends on it). A `daimon-mcp` entry naming another
    URL is the reserved name worn by a server daimon does not run; the session
    gets the real endpoint under that name instead, so the exemption can never
    cover a third party.
    """
    if not policy.enabled or not public_url:
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
) -> ToolVerdict:
    """Decide one blocked call.

    `attended` is whether a person started this turn and can answer a card
    (chat) or not (routines, wakes, smoke runs). `trusted_servers` is
    `trusted_servers_for(public_url)`; empty (the default) gates every server.
    A trusted server's call is a write only when `CONFIRMED_DAIMON_TOOLS` says so.
    """
    if not policy.enabled:
        return ToolVerdict(outcome="allow", effect="read", reason="disabled")
    if call.server_name is None:
        return ToolVerdict(outcome="allow", effect="read", reason="not_attached")
    effect: ToolEffect
    if _gated(call.server_name, trusted_servers):
        effect = classify_tool(
            policy, server_name=call.server_name, tool_name=call.tool_name, annotations=annotations
        )
    elif _confirmed_daimon_write(call):
        effect = "write"
    else:
        return ToolVerdict(outcome="allow", effect="read", reason="not_attached")
    if _listed(policy.denied, server_name=call.server_name, tool_name=call.tool_name):
        return ToolVerdict(outcome="deny", effect=effect, reason="denied_by_operator")
    if effect == "read":
        return ToolVerdict(outcome="allow", effect=effect, reason="read")
    if attended:
        return ToolVerdict(outcome="ask", effect=effect, reason="write_needs_confirmation")
    if not _confirmed_daimon_write(call) and (
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
) -> list[dict[str, Any]] | None:
    """`tools` (SDK params dicts) with every toolset's policy set, or `None`.

    `None` means nothing needs to change: enforcement is off, or every toolset
    already carries the policy this deployment wants. Otherwise each gated
    toolset gets `always_ask` as its default, and any per-tool
    `permission_policy` under it is dropped, since a per-tool `always_allow`
    would let that one tool skip the pause. Daimon's own trusted toolset
    stays `always_allow` except for its `CONFIRMED_DAIMON_TOOLS`, which ask.

    The session carries this, not the agent: `create_session` sends it as an
    `agent_with_overrides`, so however the agent was written (panel, chat
    tools, a fork, the API directly) its sessions are gated.
    """
    if not policy.enabled:
        return None
    changed = False
    out: list[dict[str, Any]] = []
    for tool in tools:
        entry = dict(tool)
        if entry.get("type") == "mcp_toolset":
            server_name = entry.get("mcp_server_name")
            wanted = toolset_permission_policy(
                policy,
                server_name=server_name if isinstance(server_name, str) else "",
                trusted_servers=trusted_servers,
            )
            default_config = dict(entry.get("default_config") or {})
            if default_config.get("permission_policy") != wanted:
                default_config["permission_policy"] = wanted
                changed = True
            entry["default_config"] = default_config
            if wanted["type"] == "always_ask" and entry.get("configs"):
                configs: list[dict[str, Any]] = []
                for config in entry["configs"]:
                    trimmed = {k: v for k, v in dict(config).items() if k != "permission_policy"}
                    changed = changed or len(trimmed) != len(config)
                    configs.append(trimmed)
                entry["configs"] = configs
            elif server_name == DAIMON_SERVER_NAME and server_name in trusted_servers:
                configs, asked = _ask_for_confirmed_tools(entry.get("configs") or [])
                changed = changed or asked
                entry["configs"] = configs
        out.append(entry)
    return out if changed else None


def _ask_for_confirmed_tools(
    configs: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """The trusted toolset's per-tool configs with each `CONFIRMED_DAIMON_TOOLS` on `always_ask`."""
    ask: dict[str, PermissionPolicyType] = {"type": "always_ask"}
    out = [dict(config) for config in configs]
    changed = False
    for name in CONFIRMED_DAIMON_TOOLS:
        config = next((c for c in out if c.get("name") == name), None)
        if config is None:
            out.append({"name": name, "permission_policy": ask})
            changed = True
        elif config.get("permission_policy") != ask:
            config["permission_policy"] = ask
            changed = True
    return out, changed

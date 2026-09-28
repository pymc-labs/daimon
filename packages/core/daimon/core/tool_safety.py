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
  `session_tools_for_policy` applies it to a session's tool list, which
  `create_session` sends as a per-session override.

With `enabled=False` (the default) nothing changes: every toolset stays
`always_allow` and every call is allowed, which is the behaviour before this
module existed.

Pure module — no I/O, no clock, no settings lookup. The shells (session
create, turn driver, adapter cards) read a policy value and a `ToolVerdict` from here
and keep their own copy and I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
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
    "session_tools_for_policy",
    "toolset_permission_policy",
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

# Leading verbs that name a read. Deliberately short: a verb that is sometimes
# a write ("run_", "sync_", "export_") stays out, so it falls to the
# fail-closed `write` default instead.
_READ_VERBS: Final[tuple[str, ...]] = (
    "get",
    "list",
    "search",
    "query",
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

# Leading verbs that name a write. Only consulted to stop a write verb's
# object from reading as a server prefix ("delete_list" is not "list").
_WRITE_VERBS: Final[frozenset[str]] = frozenset(
    {
        "add",
        "archive",
        "cancel",
        "close",
        "create",
        "delete",
        "edit",
        "insert",
        "merge",
        "move",
        "patch",
        "post",
        "publish",
        "put",
        "remove",
        "rename",
        "send",
        "set",
        "update",
        "upsert",
        "write",
    }
)


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
            "from their name (get_, list_, search_ ... are reads) and anything unknown is a "
            "write."
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
            'unattended runs, e.g. ["linear/create_issue"]. "*" allows every write there.'
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
    words = tool_name.lower().replace("-", "_").replace(".", "_").split("_")
    if words[0] in _READ_VERBS:
        return True
    if words[0] in _WRITE_VERBS:
        return False
    # A server may prefix its own name ("notion_get_page"): a read verb as the
    # second word counts when the first word is not itself a verb.
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
    `destructive_hint`; a read verb as the tool name's first word, or as its
    second word after a non-verb prefix; otherwise `write`.
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


def _gated(server_name: str | None) -> bool:
    return server_name is not None and server_name != DAIMON_SERVER_NAME


def decide_tool_call(
    policy: ToolSafetyPolicy,
    call: ToolCall,
    *,
    attended: bool,
    annotations: ToolAnnotations | None = None,
) -> ToolVerdict:
    """Decide one blocked call.

    `attended` is whether a person started this turn and can answer a card
    (chat) or not (routines, wakes, smoke runs).
    """
    if not policy.enabled:
        return ToolVerdict(outcome="allow", effect="read", reason="disabled")
    if call.server_name is None or not _gated(call.server_name):
        return ToolVerdict(outcome="allow", effect="read", reason="not_attached")
    effect = classify_tool(
        policy, server_name=call.server_name, tool_name=call.tool_name, annotations=annotations
    )
    if _listed(policy.denied, server_name=call.server_name, tool_name=call.tool_name):
        return ToolVerdict(outcome="deny", effect=effect, reason="denied_by_operator")
    if effect == "read":
        return ToolVerdict(outcome="allow", effect=effect, reason="read")
    if attended:
        return ToolVerdict(outcome="ask", effect=effect, reason="write_needs_confirmation")
    if ANY_KEY in policy.unattended_writes or _listed(
        policy.unattended_writes, server_name=call.server_name, tool_name=call.tool_name
    ):
        return ToolVerdict(outcome="allow", effect=effect, reason="unattended_write_allowed")
    return ToolVerdict(outcome="deny", effect=effect, reason="unattended_write")


def toolset_permission_policy(
    policy: ToolSafetyPolicy, *, server_name: str | None
) -> dict[str, PermissionPolicyType]:
    """The MA `permission_policy` for a toolset: `always_ask` iff gated.

    `server_name=None` is the sandbox `agent_toolset_20260401`, which always
    runs: bash and file edits inside the session's own sandbox are not
    third-party writes.
    """
    if policy.enabled and _gated(server_name):
        return {"type": "always_ask"}
    return {"type": "always_allow"}


def session_tools_for_policy(
    policy: ToolSafetyPolicy, tools: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]] | None:
    """`tools` (SDK params dicts) with every toolset's policy set, or `None`.

    `None` means nothing needs to change: enforcement is off, or every toolset
    already carries the policy this deployment wants. Otherwise each gated
    toolset gets `always_ask` as its default, and any per-tool
    `permission_policy` under it is dropped, since a per-tool `always_allow`
    would let that one tool skip the pause.

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
                policy, server_name=server_name if isinstance(server_name, str) else None
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
        out.append(entry)
    return out if changed else None

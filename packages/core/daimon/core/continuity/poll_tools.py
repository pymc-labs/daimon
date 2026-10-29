"""Code-reviewed OS polling catalogue. Never infer enrollment from a tool name.

Entries are reviewed with their adapter implementation and live authorization.
The READ and NO_WATCH flags are independent: a read may be unsuitable to poll.
No URL, shell, management, model execution, clock or external MCP tool belongs
here. Expanding this catalogue requires code review and authorization tests.

The catalogue says what *can* be polled; the operator's registry
(`DAIMON_WATCHES__POLLABLE_TOOLS`) says what *is* polled in this deployment.
A watch needs both, checked at create, at resume and before and after each read.

I1 (loops): A poll never fires on content Daimon itself wrote. Bot- and
assistant-authored rows are excluded before the human-message window and before
projection; pagination metadata never enters a poll document. A bounded scan
that cannot establish that window saves no baseline or evidence. Source IDs must
match ^[1-9][0-9]{0,19}$ at the boundary and are stored canonically; authorization
also compares resolved source and destination places.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from daimon.core.config import PollableTool, load_watches_settings
from daimon.core.continuity.watch_projection import bounded, projection_path, validate_rule
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from pydantic import JsonValue


@dataclass(frozen=True)
class PollTool:
    effect: str
    no_watch: bool
    arguments: frozenset[str]


POLL_TOOLS = MappingProxyType(
    {
        "read_thread": PollTool("read", False, frozenset({"thread_id", "limit"})),
        "read_channel": PollTool("read", False, frozenset({"channel_id", "limit"})),
    }
)


def require_poll_tool(name: str) -> PollTool:
    tool = POLL_TOOLS.get(name)
    if tool is None or tool.effect != "read" or tool.no_watch:
        raise ValueError("Tool is not reviewed for polling")
    return tool


class ToolUnlisted(ValueError):
    """The operator took the tool off the registry; pauses as `tool_unlisted`."""


def require_listed(server: str, tool: str) -> PollableTool:
    """The registry entry for (server, tool); servers compare by name or canonical URL only."""
    from daimon.core.mcp_server_url import same_ascii_mcp_url

    for entry in load_watches_settings().pollable_tools:
        if entry.tool != tool:
            continue
        if server == DAIMON_SERVER_NAME or entry.server == DAIMON_SERVER_NAME:
            if entry.server == server:
                return entry
        elif same_ascii_mcp_url(entry.server, server):
            return entry
    raise ToolUnlisted("This tool is not on the list of tools a watch can check here")


def watch_server(watch_source: str, source_state: Mapping[str, object]) -> str:
    """The registry server a stored watch polls: `daimon-mcp`, or its canonical URL."""
    return DAIMON_SERVER_NAME if watch_source == "poll" else str(source_state.get("server_url"))


class SourceChanged(ValueError):
    """The server's listing for the tool no longer matches what the owner approved."""


#: The fields of one `tools/list` entry the owner's approval covers.
CATALOGUE_FIELDS = ("name", "inputSchema", "outputSchema", "annotations")


def catalogue_digest(entry: Mapping[str, JsonValue]) -> str:
    """sha256 of the tool's listed name, schemas and annotations, in canonical JSON."""
    import hashlib
    import json

    pinned = {field: entry.get(field) for field in CATALOGUE_FIELDS}
    return hashlib.sha256(
        json.dumps(pinned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def require_read_only_listing(entry: Mapping[str, JsonValue]) -> None:
    """I4.3: the server itself must say the tool only reads. This can refuse, never allow."""
    annotations = entry.get("annotations")
    if (
        not isinstance(annotations, dict)
        or annotations.get("readOnlyHint") is not True
        or annotations.get("destructiveHint") is True
    ):
        raise SourceChanged("That tool's server does not say it only reads")


def require_schema_arguments(
    entry: Mapping[str, JsonValue], arguments: Mapping[str, JsonValue]
) -> None:
    """The arguments fit the listed `inputSchema`: known names, required present, plain types.

    A deliberately small check: names and top-level JSON types only. Values are
    bounded separately, and the server still validates them itself.
    """
    schema = entry.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise SourceChanged("That tool's arguments cannot be checked")
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise SourceChanged("That tool's arguments cannot be checked")
    if any(name not in arguments for name in required):
        raise ValueError("Poll arguments are missing a value the tool needs")
    if schema.get("additionalProperties") is not True and set(arguments) - set(properties):
        raise ValueError("Poll arguments name a value the tool does not take")
    kinds: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list,),
        "object": (dict,),
        "null": (type(None),),
    }
    for name, value in arguments.items():
        spec = properties.get(name)
        declared = spec.get("type") if isinstance(spec, dict) else None
        names = [declared] if isinstance(declared, str) else declared
        if not isinstance(names, list):
            continue
        allowed = tuple(
            kind for item in names if isinstance(item, str) for kind in kinds.get(item, ())
        )
        is_bool = isinstance(value, bool)
        if allowed and (not isinstance(value, allowed) or (is_bool and bool not in allowed)):
            raise ValueError(f"Poll argument {name} has the wrong type")


def validate_mcp_poll(
    arguments: dict[str, JsonValue],
    *,
    projection: str,
    rule: str,
    interval: int,
    key: str | None = None,
    evidence: Sequence[str] = (),
) -> None:
    """The stored shape of an MCP server poll; the server's own schema is checked live."""
    bounded(arguments)
    projection_path(projection, allow_map=True)
    if rule not in {"change", "added", "updated"}:
        raise ValueError("Unsupported poll arguments or rule")
    validate_rule(rule, key=key, evidence=evidence)
    if not 60 <= interval <= 86400:
        raise ValueError("Poll interval must be 60 to 86400 seconds")


def validate_poll(
    name: str,
    arguments: dict[str, JsonValue],
    *,
    projection: str,
    rule: str,
    interval: int,
    key: str | None = None,
    evidence: Sequence[str] = (),
) -> None:
    tool = require_poll_tool(name)
    bounded(arguments)
    projection_path(projection, allow_map=True)
    if set(arguments) - tool.arguments or rule not in {"change", "added", "updated"}:
        raise ValueError("Unsupported poll arguments or rule")
    validate_rule(rule, key=key, evidence=evidence)
    identity = "thread_id" if name == "read_thread" else "channel_id"
    value = arguments.get(identity)
    canonical_source_id(value)
    limit = arguments.get("limit", 10)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Poll read limit must be 1 to 20")
    if not 60 <= interval <= 86400:
        raise ValueError("Poll interval must be 60 to 86400 seconds")


def canonical_source_id(value: object) -> str:
    """Accept exactly one spelling; no aliases survive into registration or reads."""
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
        raise ValueError("Poll source requires a canonical message resource ID")
    return value


def require_distinct_source(arguments: Mapping[str, object], destination: str) -> None:
    """Reporting into the source would turn each report into another firing."""
    source = canonical_source_id(arguments.get("thread_id", arguments.get("channel_id")))
    if source == canonical_source_id(destination):
        raise ValueError("Poll source must differ from the reporting thread")

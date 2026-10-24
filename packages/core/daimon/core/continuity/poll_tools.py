"""Code-reviewed OS polling catalogue. Never infer enrollment from a tool name.

Entries are reviewed with their adapter implementation and live authorization.
The READ and NO_WATCH flags are independent: a read may be unsuitable to poll.
No URL, shell, management, model execution, clock or external MCP tool belongs
here. Expanding this registry requires code review and authorization tests.

I1 (loops): A poll never fires on content Daimon itself wrote. Bot- and
assistant-authored rows are excluded before the human-message window and before
projection; pagination metadata never enters a poll document. A bounded scan
that cannot establish that window saves no baseline or evidence. Source IDs must
match ^[1-9][0-9]{0,19}$ at the boundary and are stored canonically; authorization
also compares resolved source and destination places.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from daimon.core.continuity.watch_projection import bounded, projection_path
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


def validate_poll(
    name: str, arguments: dict[str, JsonValue], *, projection: str, rule: str, interval: int
) -> None:
    tool = require_poll_tool(name)
    bounded(arguments)
    projection_path(projection)
    if set(arguments) - tool.arguments or rule not in {"change", "added", "updated"}:
        raise ValueError("Unsupported poll arguments or rule")
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

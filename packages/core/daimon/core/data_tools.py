"""The `data_*` tools `data_reads` offers, and how their results stay out of other readers' hands.

Every data tool result starts with `DATA_RESULT_MARK` (`mark_data_result`), so a transcript
read outside its own session (a hub admin's, another session's) shows it redacted
(`redact_data_results`) whichever page holds the call. Pure.
"""

from __future__ import annotations

from typing import Final

from pydantic import JsonValue

DATA_READS: Final = "data_reads"
DATA_TOOLS: Final[frozenset[str]] = frozenset(
    {"data_sources", "data_search", "data_get", "data_file", "data_sql"}
)
"""The data tools registered behind `data_reads` (`daimon.adapters.mcp.tools.data`)."""
PERSONAL_DRIVE: Final = "data_personal_drive"
PERSONAL_CALENDAR: Final = "data_personal_calendar"
PERSONAL_PARTS: Final[dict[str, str]] = {"drive": PERSONAL_DRIVE, "calendar": PERSONAL_CALENDAR}
"""A person's personal part to the feature a workspace admin turns it off with."""
PERSONAL_DRIVE_TOOLS: Final[frozenset[str]] = frozenset({"data_my_drive", "data_drive_reads"})
PERSONAL_CALENDAR_TOOLS: Final[frozenset[str]] = frozenset({"data_calendar_reads"})
"""A person's own Drive read as them, each read on their card (`daimon.data.my_drive`), and
each person's own switch for a part. They need no `data_reads` and stay in rooms outsiders
may read, but only a workspace with a Google source is offered them (`place_features`)."""
DATA_SOURCES: Final = "data_sources"
DATA_SOURCE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "list_data_sources",
        "change_data_source",
        "list_data_access",
        "change_data_access",
        "list_data_links",
        "change_data_links",
        "list_data_rule_sets",
        "approve_data_rule_set",
        "connect_my_data",
        "check_my_data_connection",
        "dismiss_data_notices",
        "send_private_details",
    }
)
"""The data management and sign-in tools: hidden with `data_sources`, and until a workspace has a
source (`daimon.core.ports.data_ready`) but for `DATA_SETUP_TOOLS`."""
DATA_SETUP_TOOLS: Final[frozenset[str]] = frozenset({"list_data_sources", "change_data_source"})
"""What adds the first source: offered (to admins, by their `admin` tag) while the feature is on."""
DATA_RESULT_MARK: Final = "[data source result]"
REDACTED_DATA_RESULT: Final = f"{DATA_RESULT_MARK} hidden: only its own conversation sees it."


def mark_data_result(text: str) -> str:
    return f"{DATA_RESULT_MARK}\n{text}"


def redact_data_results(value: JsonValue) -> JsonValue:
    """`value` (a dumped event) with every string that carries a data result replaced."""
    if isinstance(value, str):
        return REDACTED_DATA_RESULT if DATA_RESULT_MARK in value else value
    if isinstance(value, list):
        return [redact_data_results(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data_results(item) for key, item in value.items()}
    return value

"""Defaults reconciliation library — `apply_defaults` and friends."""

from daimon.core.defaults.apply import apply_defaults
from daimon.core.defaults.mcp_merge import (
    DAIMON_MCP_SERVER_NAME,
    merge_default_mcp_server,
    merge_default_mcp_toolset,
)
from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome

__all__ = [
    "apply_defaults",
    "ApplyReport",
    "ResourceOutcome",
    "Action",
    "DAIMON_MCP_SERVER_NAME",
    "merge_default_mcp_server",
    "merge_default_mcp_toolset",
]

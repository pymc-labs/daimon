"""Black-box conformance kit for ``daimon-harness/0``: point it at a base URL and a fake model.

``run_conformance(HarnessTarget(...))`` exercises turn lifecycle, idempotent replay, event
cursors, approvals, cancel, restart without re-run, usage metering, skills, MCP and tool
projection over HTTP, and reports PASS, SKIP (with a reason) or FAIL per case. Cases that
need a capability the caller did not supply (a restart hook) are skipped, never passed.
"""

from .cases import CASES, Case
from .client import HarnessClient, parse_sse
from .runner import format_report, run_conformance
from .target import CaseResult, HarnessTarget, ModelScripter, RestartHook, SkipCase

__all__ = [
    "CASES",
    "Case",
    "CaseResult",
    "HarnessClient",
    "HarnessTarget",
    "ModelScripter",
    "RestartHook",
    "SkipCase",
    "format_report",
    "parse_sse",
    "run_conformance",
]

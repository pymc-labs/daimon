"""Every agent lookup passes the Daimon-owned records, or says why it is Anthropic-only."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]
# Environments route through the records seam too, but their sweep is a later step.
AGENT_WRAPPERS = {
    "find_agent_by_daimon_tag",
    "find_agents_by_daimon_tag",
    "list_agents_by_tenant",
    "find_agent_by_derived_uuid",
    "retrieve_agent",
    "retrieve_agent_version",
    "update_agent",
    "update_agent_with_version_retry",
    "archive_agent",
    "create_agent",
    "list_agents",
}
SKILLS = "skill ids exist only on Anthropic; the agent is read to attach one"
SESSIONS = "session, vault, output and extension ports exist only on Anthropic"
# Files whose bare-name agent lookups stay Anthropic-only, with the reason.
WRAPPER_ALLOWED = {
    "core/daimon/core/defaults/": "platform seeding reconciles Anthropic agents by tag",
    "core/daimon/core/skills/": SKILLS,
    "core/daimon/core/skill_sync/": SKILLS,
    "core/daimon/core/server_skills/": SKILLS,
    "core/daimon/core/reader_agent.py": "the reader agent is an Anthropic-hosted helper",
    "core/daimon/core/long_jobs_ma.py": "long jobs run as Anthropic sessions",
    "core/daimon/core/agent_fork.py": "forking an Anthropic agent; Daimon-owned forks use records",
    "core/daimon/core/ma_resolver.py": "resolves Anthropic agents by name; dm_ ids use records",
    "adapters/web/": "web adapter sweep is separate",
}
# Files whose literal managed_agents( ports stay Anthropic-only, with the reason.
PORT_ALLOWED = {
    "core/daimon/core/ma.py": "thin Anthropic helpers",
    "core/daimon/core/mux_compat.py": "the wrappers take records and build the routed port",
    "core/daimon/core/session_ports_compat.py": SESSIONS,
    "core/daimon/core/output_ports_compat.py": SESSIONS,
    "core/daimon/core/workspace_ports_compat.py": SESSIONS,
    "core/daimon/core/session_preparation.py": SESSIONS,
    "core/daimon/core/turn/": SESSIONS,
    "core/daimon/core/headless_runner.py": SESSIONS,
    "core/daimon/core/long_jobs_ma.py": SESSIONS,
    "core/daimon/core/pending_file_sweeper.py": SESSIONS,
    "core/daimon/core/usage_sweep.py": SESSIONS,
    "core/daimon/core/workspace_transfer.py": "workspace files are Anthropic session resources",
    "core/daimon/core/agent_convert.py": "reads the Anthropic source agent it converts",
    "core/daimon/core/defaults/platform_export.py": "exports platform seed agents from Anthropic",
    "adapters/cli/daimon/adapters/cli/mux_compat.py": "wrappers take records",
    "adapters/mcp/daimon/adapters/mcp/resource_ports.py": SESSIONS,
    "adapters/discord/daimon/adapters/discord/startup_replay.py": SESSIONS,
}


def _sources() -> list[Path]:
    roots = [PACKAGES / "core/daimon", *sorted((PACKAGES / "adapters").glob("*/daimon*"))]
    return [path for root in roots for path in root.rglob("*.py")]


def _allowed(path: Path, table: dict[str, str]) -> bool:
    rel = path.relative_to(PACKAGES).as_posix()
    return any(rel == key or (key.endswith("/") and rel.startswith(key)) for key in table)


def _offenders(names: set[str], table: dict[str, str]) -> list[str]:
    found: list[str] = []
    for path in _sources():
        if _allowed(path, table):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id not in names:
                continue
            if any(kw.arg in {"records", None} for kw in node.keywords):
                continue
            found.append(f"{path.relative_to(PACKAGES)}:{node.lineno} {func.id}")
    return found


def test_agent_lookups_pass_records() -> None:
    offenders = _offenders(AGENT_WRAPPERS, WRAPPER_ALLOWED)
    assert not offenders, (
        "agent lookups without records= cannot see Daimon-owned agents; pass records= "
        f"or allowlist the file with a reason: {offenders}"
    )


def test_managed_agents_ports_pass_records() -> None:
    offenders = _offenders({"managed_agents"}, PORT_ALLOWED)
    assert not offenders, (
        "managed_agents( without records= builds an Anthropic-only port; pass records= "
        f"or allowlist the file with a reason: {offenders}"
    )


def test_allowlists_name_live_files() -> None:
    for table in (WRAPPER_ALLOWED, PORT_ALLOWED):
        for key in table:
            assert (PACKAGES / key).exists(), f"allowlist entry {key} matches no file"

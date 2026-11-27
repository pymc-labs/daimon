"""Generate docs/features.md from the feature registry.

Run: uv run python scripts/generate_feature_reference.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features.deployment import deployment_value
from daimon.core.features.registry import EVERY_PLATFORM, FEATURES, FeatureSpec
from daimon.core.stores.domain import CHAT_PLATFORMS
from pydantic import PostgresDsn, SecretStr

# The siblings below import the same way run as a script or imported as `scripts.<name>`.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _knowledge import web_only

DOCS = Path(__file__).resolve().parents[1] / "docs"
OUT = DOCS / "features.md"
_TIER = {
    "deployment": "Deployment",
    "workspace": "Workspace",
    "agent": "Agent",
    "channel": "Channel",
    "thread": "Thread",
    "routine": "Routine",
    "workflow": "Workflow",
}
_BACKEND = {"gemini": "Gemini", "openai": "OpenAI", "selfhosted": "self-hosted"}
_CATEGORY = {
    "model_input": "Model input",
    "chat": "Chat",
    "capabilities": "Capability",
    "limits": "Limit",
    "guardrails": "Guardrail",
}
_COMBINE = {
    "most_specific": "Most specific wins",
    "ceiling": "Off above is a ceiling",
    "stricter": "Stricter wins",
    "minimum": "Lowest wins",
}
_INTRO = """# Features

Every feature is set at one or more levels: Deployment, Workspace, Agent, Channel and
Thread. Each level is On, Off or Default, or one of the feature's values. Default
inherits from the level above, and at the top it is the feature's built-in default.
Settings show which level decided.

An admin can lock a value at the Deployment, Workspace, Agent or Channel level. Levels
below then cannot change it. A locked Off turns the feature off below.

Set the deployment level with `DAIMON_FEATURES__<KEY>`, for example
`DAIMON_FEATURES__PROGRESS_PLANS=on:locked`; a dot in a key is an
underscore (`DAIMON_FEATURES__TOOL_BASH`). Set the others with `/settings` in chat,
the `change_setting` MCP tool or `daimon features`.
<!-- knowledge: web -->
The web app sets them too.
<!-- /knowledge -->

A chat conversation picks up a change to a feature that hides tools at its next message.
A CLI session, or an agent chat continued with `send_message`, keeps the tools it started
with; Daimon refuses a tool turned Off since then.
"""


_BARE = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("x")),
        "discord": DiscordSettings(bot_token=SecretStr("x")),
    }
)
"""A deployment where the operator set nothing."""


def _locked_until_set(spec: FeatureSpec) -> str | None:
    """The deployment value a feature holds, locked, until the operator sets it."""
    found = deployment_value(_BARE, spec) if "deployment" in spec.tiers else None
    return spec.values.get(found.value, found.value) if found is not None and found.locked else None


def _notes(spec: FeatureSpec) -> str:
    notes: list[str] = []
    if (held := _locked_until_set(spec)) is not None:
        notes.append(f"{held} and locked until whoever runs Daimon turns it on")
    if any(need != "platform_adapter" for need in spec.requires):
        notes.append("Needs setup by whoever runs Daimon")
    if spec.platforms != EVERY_PLATFORM and set(spec.platforms) != set(CHAT_PLATFORMS):
        notes.append("Only " + ", ".join(sorted(p.title() for p in spec.platforms)))
    for platform, value in sorted(spec.platform_defaults.items()):
        notes.append(f"{spec.values.get(value, value)} by default on {platform.title()}")
    by_note: dict[str, list[str]] = {}
    for backend, tiers in sorted(spec.backend_tiers.items()):
        note = f"Up to {_TIER[tiers[-1]]} on" if tiers else "Not on"
        by_note.setdefault(note, []).append(_BACKEND.get(backend, backend))
    for backend, value in sorted(spec.backend_fixed.items()):
        note = f"Always {spec.values.get(value, value)} on"
        by_note.setdefault(note, []).append(_BACKEND.get(backend, backend))
    notes += [f"{note} {', '.join(backends)}" for note, backends in by_note.items()]
    return "; ".join(notes)


def _is_web(spec: FeatureSpec) -> bool:
    return spec.key == "web_awareness" or spec.key.startswith("web.")


def _link(spec: FeatureSpec) -> str:
    """The feature's page; its own section here is a link within this page."""
    return spec.page.removeprefix("features.md")


def _page_title(page: str) -> str:
    heading = ("\n" + (DOCS / page.partition("#")[0]).read_text()).split("\n# ", 1)
    return heading[1].partition("\n")[0] if len(heading) > 1 else page


def _table(specs: list[FeatureSpec]) -> list[str]:
    head = "| Feature | Key | Kind | Default | " + " | ".join(_TIER.values()) + " | Notes |"
    rows = [head, "|" + " --- |" * (len(_TIER) + 5)]
    for spec in sorted(specs, key=lambda s: list(_CATEGORY).index(s.category)):
        levels = " | ".join("✓" if tier in spec.tiers else "" for tier in _TIER)
        default = spec.values.get(spec.default, spec.default)
        rows.append(
            f"| [{spec.name}]({_link(spec)}) | `{spec.key}` | {_CATEGORY[spec.category]} | "
            f"{default} | {levels} | {_notes(spec)} |"
        )
    return rows


def summary() -> str:
    intro = "## All features\n\nWhere each feature can be set, and its default.\n\n"
    every = list(FEATURES.values())
    web = [spec for spec in every if _is_web(spec)]
    lines = _table([spec for spec in every if not _is_web(spec)])
    return intro + "\n".join([*lines, "", *web_only(["", *_table(web), ""])]) + "\n"


def _section(spec: FeatureSpec) -> str:
    values = ", ".join(f"`{key}` ({label})" for key, label in spec.values.items())
    if spec.max_length:
        values += f", or any name up to {spec.max_length} characters"
    rows = [
        ("Key", f"`{spec.key}`"),
        ("Values", values),
        ("Default", f"`{spec.default}`"),
        ("Levels", ", ".join(_TIER[tier] for tier in spec.tiers)),
        ("Combines", _COMBINE[spec.combine]),
    ]
    for backend, tiers in sorted(spec.backend_tiers.items()):
        levels = ", ".join(_TIER[tier] for tier in tiers) or "Not available"
        rows.append((f"Levels on {_BACKEND.get(backend, backend)}", levels))
    if spec.legacy_env:
        rows.append(("Deprecated", ", ".join(f"`{env}`" for env in spec.legacy_env)))
    if not spec.page.startswith("features.md#"):
        rows.append(("Explained in", f"[{_page_title(spec.page)}]({spec.page})"))
    table = "\n".join(f"| {name} | {value} |" for name, value in rows)
    return f"## {spec.name}\n\n{spec.doc}\n\n| | |\n| --- | --- |\n{table}\n"


def render() -> str:
    parts = [_INTRO, summary()]
    web: list[str] = []
    for spec in FEATURES.values():
        if not _is_web(spec):
            parts.append(_section(spec))
        else:
            web.append(_section(spec))
    parts.append("\n".join(web_only(["", "\n".join(web)])))
    return "\n".join(parts)


def main() -> int:
    OUT.write_text(render())  # built by the docs workflow, never committed
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

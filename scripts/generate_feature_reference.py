"""Generate docs/features.md from the feature registry.

Run: uv run python scripts/generate_feature_reference.py [--check]
"""

from __future__ import annotations

import sys
from pathlib import Path

from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features.deployment import deployment_value
from daimon.core.features.registry import EVERY_PLATFORM, FEATURES, FeatureSpec
from daimon.core.stores.domain import CHAT_PLATFORMS
from pydantic import PostgresDsn, SecretStr

OUT = Path(__file__).resolve().parents[1] / "docs" / "features.md"
_TIER = {
    "deployment": "Deployment",
    "workspace": "Workspace",
    "agent": "Agent",
    "channel": "Channel",
    "thread": "Thread",
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
below then cannot change it. `disabled` means Off and locked.

Set the deployment level with `DAIMON_FEATURES__<KEY>`, for example
`DAIMON_FEATURES__PROGRESS_PLANS=on:locked`; a dot in a key is an
underscore (`DAIMON_FEATURES__TOOL_BASH`). Set the others with `/settings` in chat,
the `change_setting` MCP tool, the web app or `daimon features`.
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
        notes.append(f"{held} and locked until the operator turns it on")
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


def summary() -> str:
    head = "| Feature | Key | Kind | Default | " + " | ".join(_TIER.values()) + " | Notes |"
    rows = [head, "|" + " --- |" * (len(_TIER) + 5)]
    for spec in sorted(FEATURES.values(), key=lambda s: list(_CATEGORY).index(s.category)):
        levels = " | ".join("✓" if tier in spec.tiers else "" for tier in _TIER)
        default = spec.values.get(spec.default, spec.default)
        rows.append(
            f"| {spec.name} | `{spec.key}` | {_CATEGORY[spec.category]} | {default} | "
            f"{levels} | {_notes(spec)} |"
        )
    intro = "## All features\n\nWhere each feature can be set, and its default.\n\n"
    return intro + "\n".join(rows) + "\n"


def render() -> str:
    parts = [_INTRO, summary()]
    for spec in FEATURES.values():
        values = ", ".join(f"`{key}` ({label})" for key, label in spec.values.items())
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
        table = "\n".join(f"| {name} | {value} |" for name, value in rows)
        parts.append(f"## {spec.name}\n\n{spec.doc}\n\n| | |\n| --- | --- |\n{table}\n")
    return "\n".join(parts)


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT} is stale: run scripts/generate_feature_reference.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

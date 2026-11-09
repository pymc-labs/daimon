"""Generate docs/features.md from the feature registry.

Run: uv run python scripts/generate_feature_reference.py [--check]
"""

from __future__ import annotations

import sys
from pathlib import Path

from daimon.core.features.registry import FEATURES

OUT = Path(__file__).resolve().parents[1] / "docs" / "features.md"
_TIER = {
    "deployment": "Deployment",
    "workspace": "Server",
    "agent": "Agent",
    "channel": "Channel",
    "thread": "Thread",
}
_COMBINE = {
    "most_specific": "Most specific wins",
    "ceiling": "Off above is a ceiling",
    "stricter": "Stricter wins",
    "minimum": "Lowest wins",
}
_INTRO = """# Features

Every feature is set at one or more levels: deployment, server, agent, channel and
thread. Each level is On, Off or Default, or one of the feature's values. Default
inherits from the level above. Settings show which level decided.

An admin can lock a value at the deployment, server, agent or channel level. Levels
below then cannot change it. `disabled` means Off and locked.

Set the deployment level with `DAIMON_FEATURES__<KEY>`, for example
`DAIMON_FEATURES__PROGRESS_PLANS=on:locked`. Set the others with `/settings` in chat,
the `change_setting` MCP tool or the web app.
"""


def render() -> str:
    parts = [_INTRO]
    for spec in FEATURES.values():
        values = ", ".join(f"`{key}` ({label})" for key, label in spec.values.items())
        rows = [
            ("Key", f"`{spec.key}`"),
            ("Values", values),
            ("Default", f"`{spec.default}`"),
            ("Levels", ", ".join(_TIER[tier] for tier in spec.tiers)),
            ("Combines", _COMBINE[spec.combine]),
        ]
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

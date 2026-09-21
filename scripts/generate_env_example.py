"""Generate .env.example from the live Settings + SchedulerSettings shape.

Walks `daimon.core.config.Settings.model_fields` and
`daimon.adapters.scheduler.settings.SchedulerSettings.model_fields`
recursively (unwrapping optional nested blocks) to build a complete, tiered
`.env.example` covering every env var the app actually reads, plus the flat
Stripe billing vars (billing.py:load_billing_config) and the
docker-compose-only Postgres vars.

The walk itself lives in `_settings_walk.py`, shared with
`generate_config_reference.py` so the two pages can never disagree about
which env vars exist.

Run: uv run python scripts/generate_env_example.py [--check]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from _settings_walk import (
    ADAPTER_REQUIRED_NOTES,
    BILLING_FLAT_VARS,
    ENV_DEPENDENT_PLACEHOLDERS,
    SettingsLeaf,
    collect_leaves,
    is_secret_annotation,
    section_title,
    split_top_level,
    stringify_default,
)
from daimon.adapters.scheduler.settings import SchedulerSettings
from daimon.core.config import Settings
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

# Vars that are always required to boot the app at all — uncommented at the
# top of the file.
ALWAYS_REQUIRED = {
    "DAIMON_ANTHROPIC__API_KEY",
    "DAIMON_DATABASE__URL",
}

# Sample values for required fields that carry no schema default (there is
# nothing else to render after '=' for these).
EXAMPLE_VALUES: dict[str, str] = {
    "DAIMON_DATABASE__URL": "postgresql+asyncpg://daimon:daimon@localhost:5432/daimon",
}


@dataclass(frozen=True)
class EnvVar:
    name: str
    description: str
    default_line: str
    is_secret: bool
    note: str | None
    uncommented: bool


@dataclass(frozen=True)
class Section:
    title: str
    variables: list[EnvVar]


def _render_default(env_name: str, field: FieldInfo, is_secret: bool) -> str:
    """Render the text that goes after '=' — never the evaluated value of an
    env-dependent default_factory (Pitfall 7), never a secret value."""
    if env_name in EXAMPLE_VALUES:
        return EXAMPLE_VALUES[env_name]
    if env_name in ENV_DEPENDENT_PLACEHOLDERS or is_secret:
        return ""
    if field.default_factory is not None:
        # mypy/pyright: default_factory is Callable[[], Any] on FieldInfo;
        # every non-placeholder factory in this codebase is a pure
        # constructor (e.g. `lambda: Path("defaults")`) with no env reads.
        value = field.default_factory()  # pyright: ignore[reportCallIssue]
        return stringify_default(value)
    if field.default is None or field.default is PydanticUndefined:
        return ""
    return stringify_default(field.default)


def _build_env_var(leaf: SettingsLeaf) -> EnvVar:
    is_secret = is_secret_annotation(leaf.field.annotation)
    description = leaf.field.description or leaf.field_name
    note = ENV_DEPENDENT_PLACEHOLDERS.get(leaf.env_name) or ADAPTER_REQUIRED_NOTES.get(
        leaf.env_name
    )
    return EnvVar(
        name=leaf.env_name,
        description=description,
        default_line=_render_default(leaf.env_name, leaf.field, is_secret),
        is_secret=is_secret,
        note=note,
        uncommented=leaf.env_name in ALWAYS_REQUIRED,
    )


def _build_sections() -> list[Section]:
    core_leaves, blocks = split_top_level(Settings, "DAIMON_")
    sections = [Section(title="Core", variables=[_build_env_var(v) for v in core_leaves])]
    sections.extend(
        Section(
            title=section_title(block.field_name),
            variables=[_build_env_var(v) for v in block.leaves],
        )
        for block in blocks
    )
    scheduler_leaves = collect_leaves(SchedulerSettings, "DAIMON_SCHEDULER__")
    sections.append(
        Section(title="Scheduler", variables=[_build_env_var(v) for v in scheduler_leaves])
    )
    return sections


def _format_var(var: EnvVar) -> list[str]:
    tags = [t for t in (var.note, "secret" if var.is_secret else None) if t]
    tag_suffix = f" ({'; '.join(tags)})" if tags else ""
    lines = [f"# {var.description}{tag_suffix}"]
    prefix = "" if var.uncommented else "# "
    lines.append(f"{prefix}{var.name}={var.default_line}")
    return lines


def render_env_example() -> str:
    sections = _build_sections()
    lines: list[str] = []

    required_vars = [v for s in sections for v in s.variables if v.uncommented]
    lines.append("# === Required ===")
    for var in required_vars:
        lines.extend(_format_var(var))
    lines.append("")

    for section in sections:
        optional_vars = [v for v in section.variables if not v.uncommented]
        if not optional_vars:
            continue
        lines.append(f"# === {section.title} (optional) ===")
        for var in optional_vars:
            lines.extend(_format_var(var))
        lines.append("")

    lines.append("# === Billing (optional — Stripe top-ups) ===")
    lines.append("# 7-key flat env read by daimon.core.billing.load_billing_config.")
    lines.append("# No DAIMON_ prefix. Billing is disabled (not an error) when any is unset.")
    lines.append("# With billing disabled, /billing top-ups can't create a checkout. Credit")
    lines.append("# manually: insert a tenant_ledger row (delta_usd > 0, reason='topup', a")
    lines.append("# unique idempotency_key — its unique index makes a re-run a no-op).")
    for name in BILLING_FLAT_VARS:
        lines.append(f"# {name}=")
    lines.append("")

    lines.append("# === Docker Compose (not read by the app) ===")
    lines.append("POSTGRES_USER=daimon")
    lines.append("# Required by docker-compose.yml. Set a strong, URL-safe value")
    lines.append("# (avoid @ : / % # — it is interpolated raw into the asyncpg DSN).")
    lines.append("# POSTGRES_PASSWORD=")
    lines.append("POSTGRES_DB=daimon")
    lines.append("POSTGRES_PORT=5432")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate .env.example from the Settings/SchedulerSettings shape.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed .env.example matches generator output; exit nonzero on drift.",
    )
    args = parser.parse_args(argv)

    generated = render_env_example()

    if args.check:
        current = ENV_EXAMPLE_PATH.read_text() if ENV_EXAMPLE_PATH.exists() else ""
        if current != generated:
            print(
                ".env.example is out of date. Run: uv run python scripts/generate_env_example.py",
                file=sys.stderr,
            )
            return 1
        return 0

    ENV_EXAMPLE_PATH.write_text(generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())

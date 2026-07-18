"""Generate .env.example from the live Settings + SchedulerSettings shape.

Walks `daimon.core.config.Settings.model_fields` and
`daimon.adapters.scheduler.settings.SchedulerSettings.model_fields`
recursively (unwrapping optional nested blocks) to build a complete, tiered
`.env.example` covering every env var the app actually reads, plus the flat
Stripe billing vars (billing.py:load_billing_config) and the
docker-compose-only Postgres vars.

Run: uv run python scripts/generate_env_example.py [--check]
"""

from __future__ import annotations

import argparse
import sys
import typing
from dataclasses import dataclass
from pathlib import Path

from daimon.adapters.scheduler.settings import SchedulerSettings
from daimon.core.config import Settings
from pydantic import BaseModel, HttpUrl, SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

# Vars whose default is derived from the machine/process environment the
# generator happens to run on (e.g. os.environ.get("USER", ...)). Rendered as
# a stable placeholder comment instead of the evaluated value so --check
# never flaps across machines/CI (Pitfall 7).
ENV_DEPENDENT_PLACEHOLDERS: dict[str, str] = {
    "DAIMON_CLI__LOCAL_USER": "defaults to $USER",
}

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

# Vars required only when running a specific adapter — stay commented, but
# carry a "required to run the X adapter" note.
ADAPTER_REQUIRED_NOTES: dict[str, str] = {
    "DAIMON_MCP__JWT_SECRET": "required to run the MCP adapter",
    "DAIMON_MCP__PUBLIC_URL": "required to run the MCP adapter",
    "DAIMON_DISCORD__BOT_TOKEN": "required to run the Discord adapter",
    "DAIMON_SLACK__SIGNING_SECRET": "required to run the Slack adapter",
    "DAIMON_SLACK__APP_TOKEN": "required to run the Slack adapter",
}

# Human-friendly section titles for nested settings blocks, keyed by the
# Settings field name that holds them. Falls back to a title-cased version of
# the field name when a block is added without updating this map.
SECTION_TITLES: dict[str, str] = {
    "database": "Database",
    "anthropic": "Anthropic",
    "cli": "CLI",
    "log": "Logging",
    "mcp": "MCP Server",
    "discord": "Discord",
    "slack": "Slack",
    "github": "GitHub",
    "crypto": "Crypto",
    "credentials": "Credentials",
    "gemini": "Gemini",
    "notebook": "Notebook Host",
    "sentry": "Sentry",
    "billing": "Billing Policy",
}

# The 7-key flat billing env vars consumed by billing.py:load_billing_config.
# Not part of Settings/model_fields — no DAIMON_ prefix, and billing is
# disabled (not an error) when any of these is unset.
BILLING_FLAT_VARS: tuple[str, ...] = (
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRICE_10_USD",
    "STRIPE_PRICE_25_USD",
    "STRIPE_PRICE_50_USD",
    "STRIPE_PRICE_100_USD",
    "MCP_PUBLIC_URL",
)


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


def _unwrap_nested_model(annotation: object) -> type[BaseModel] | None:
    """Return the nested BaseModel type if `annotation` is a BaseModel, or an
    `X | None` union wrapping one; otherwise None (leaf/scalar field)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _is_secret_annotation(annotation: object) -> bool:
    if annotation is SecretStr:
        return True
    return any(_is_secret_annotation(arg) for arg in typing.get_args(annotation))


def _stringify_default(value: object) -> str:
    if isinstance(value, tuple):
        items = typing.cast("tuple[object, ...]", value)
        return ",".join(_stringify_default(v) for v in items)
    if isinstance(value, HttpUrl):
        return str(value).rstrip("/")
    return str(value)


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
        return _stringify_default(value)
    if field.default is None or field.default is PydanticUndefined:
        return ""
    return _stringify_default(field.default)


def _build_env_var(env_name: str, field_name: str, field: FieldInfo) -> EnvVar:
    is_secret = _is_secret_annotation(field.annotation)
    description = field.description or field_name
    note = ENV_DEPENDENT_PLACEHOLDERS.get(env_name) or ADAPTER_REQUIRED_NOTES.get(env_name)
    return EnvVar(
        name=env_name,
        description=description,
        default_line=_render_default(env_name, field, is_secret),
        is_secret=is_secret,
        note=note,
        uncommented=env_name in ALWAYS_REQUIRED,
    )


def _collect_leaves(model: type[BaseModel], prefix: str) -> list[EnvVar]:
    """Depth-first walk of `model.model_fields`, unwrapping nested optional
    settings blocks, returning one EnvVar per leaf (scalar) field."""
    leaves: list[EnvVar] = []
    for field_name, field in model.model_fields.items():
        env_name = f"{prefix}{field_name.upper()}"
        nested = _unwrap_nested_model(field.annotation)
        if nested is not None:
            leaves.extend(_collect_leaves(nested, env_name + "__"))
        else:
            leaves.append(_build_env_var(env_name, field_name, field))
    return leaves


def _build_sections() -> list[Section]:
    sections: list[Section] = []
    core_vars: list[EnvVar] = []

    for field_name, field in Settings.model_fields.items():
        env_name = f"DAIMON_{field_name.upper()}"
        nested = _unwrap_nested_model(field.annotation)
        if nested is not None:
            title = SECTION_TITLES.get(field_name, field_name.replace("_", " ").title())
            variables = _collect_leaves(nested, env_name + "__")
            sections.append(Section(title=title, variables=variables))
        else:
            core_vars.append(_build_env_var(env_name, field_name, field))

    sections.insert(0, Section(title="Core", variables=core_vars))
    scheduler_vars = _collect_leaves(SchedulerSettings, "DAIMON_SCHEDULER__")
    sections.append(Section(title="Scheduler", variables=scheduler_vars))
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
    for name in BILLING_FLAT_VARS:
        lines.append(f"# {name}=")
    lines.append("")

    lines.append("# === Docker Compose (not read by the app) ===")
    lines.append("POSTGRES_USER=daimon")
    lines.append("POSTGRES_PASSWORD=daimon")
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

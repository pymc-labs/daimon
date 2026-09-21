"""Shared pydantic-settings walk behind `.env.example` and `docs/configuration.md`.

`generate_env_example.py` and `generate_config_reference.py` render the same
env surface two different ways, so the recursion, the secret detection, the
default stringification and the per-block facts that neither model can carry
live here once. Adding a nested settings block means updating `SECTION_TITLES`
here and both pages pick the title up.

Not a script — no `main()`, nothing to run. Both importers are executed as
`uv run python scripts/<name>.py`, which puts `scripts/` on `sys.path`, so
they import this module by its bare name.
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass

from pydantic import BaseModel, HttpUrl, SecretStr
from pydantic.fields import FieldInfo

# Vars whose default is derived from the machine/process environment the
# generator happens to run on (e.g. os.environ.get("USER", ...)). Rendered as
# a stable placeholder instead of the evaluated value so --check never flaps
# across machines/CI (Pitfall 7). The value is the note `.env.example` shows;
# `docs/configuration.md` only needs the membership.
ENV_DEPENDENT_PLACEHOLDERS: dict[str, str] = {
    "DAIMON_CLI__LOCAL_USER": "defaults to $USER",
}

# Vars that are optional on the model — so nothing in the schema says so —
# but that the adapter's own boot-time validation rejects as missing.
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
    "report_host": "Report Host",
    "sentry": "Sentry",
    "billing": "Billing Policy",
    "support": "Support",
    "thread_naming": "Thread Naming",
    "artifacts": "Artifacts",
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
class SettingsLeaf:
    """One scalar settings field and the env var name that reaches it."""

    env_name: str
    field_name: str
    field: FieldInfo


@dataclass(frozen=True)
class SettingsBlock:
    """One nested settings model reached through a parent field."""

    field_name: str
    env_prefix: str
    model: type[BaseModel]
    field: FieldInfo
    leaves: tuple[SettingsLeaf, ...]

    @property
    def is_absent_by_default(self) -> bool:
        """True for a `X | None = None` block: the whole block stays unset
        until at least one of its variables is, and the code that reads it
        has to handle None."""
        return types.NoneType in typing.get_args(self.field.annotation)


def unwrap_nested_model(annotation: object) -> type[BaseModel] | None:
    """Return the nested BaseModel type if `annotation` is a BaseModel, or an
    `X | None` union wrapping one; otherwise None (leaf/scalar field)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def is_secret_annotation(annotation: object) -> bool:
    """True when the field holds a SecretStr, directly or inside a union or
    container (`SecretStr | None`, `tuple[SecretStr, ...]`)."""
    if annotation is SecretStr:
        return True
    return any(is_secret_annotation(arg) for arg in typing.get_args(annotation))


def stringify_default(value: object) -> str:
    """Render a schema default as the text an operator would put in `.env`."""
    if isinstance(value, tuple):
        items = typing.cast("tuple[object, ...]", value)
        return ",".join(stringify_default(v) for v in items)
    if isinstance(value, HttpUrl):
        return str(value).rstrip("/")
    return str(value)


def collect_leaves(model: type[BaseModel], prefix: str) -> list[SettingsLeaf]:
    """Depth-first walk of `model.model_fields`, unwrapping nested optional
    settings blocks, returning one leaf per scalar field."""
    leaves: list[SettingsLeaf] = []
    for field_name, field in model.model_fields.items():
        env_name = f"{prefix}{field_name.upper()}"
        nested = unwrap_nested_model(field.annotation)
        if nested is not None:
            leaves.extend(collect_leaves(nested, env_name + "__"))
        else:
            leaves.append(SettingsLeaf(env_name=env_name, field_name=field_name, field=field))
    return leaves


def split_top_level(
    model: type[BaseModel], prefix: str
) -> tuple[list[SettingsLeaf], list[SettingsBlock]]:
    """Split `model`'s own fields into scalars and nested blocks, in
    declaration order — the order both pages render sections in."""
    scalars: list[SettingsLeaf] = []
    blocks: list[SettingsBlock] = []
    for field_name, field in model.model_fields.items():
        env_name = f"{prefix}{field_name.upper()}"
        nested = unwrap_nested_model(field.annotation)
        if nested is None:
            scalars.append(SettingsLeaf(env_name=env_name, field_name=field_name, field=field))
            continue
        blocks.append(
            SettingsBlock(
                field_name=field_name,
                env_prefix=env_name + "__",
                model=nested,
                field=field,
                leaves=tuple(collect_leaves(nested, env_name + "__")),
            )
        )
    return scalars, blocks


def section_title(field_name: str) -> str:
    """The display title for a nested block, or a title-cased fallback."""
    return SECTION_TITLES.get(field_name, field_name.replace("_", " ").title())

"""Generate docs/configuration.md from the live settings models.

Covers the same env surface as `.env.example` — `daimon.core.config.Settings`,
`daimon.adapters.scheduler.settings.SchedulerSettings`, the flat Stripe
billing vars (billing.py:load_billing_config) and the docker-compose-only
Postgres vars — and adds the two standalone services `.env.example` leaves
out: `apps/notebook-host` and `apps/report-host`, each of which carries its
own `BaseSettings` with its own env prefix.

Where `.env.example` is a copy-paste starting point, this page is the
reference: per variable, the type, whether it is required, the default and
the field's own description. The walk is shared with
`generate_env_example.py` (`_settings_walk.py`) so neither page can list a
variable the other does not.

The two app settings modules are loaded from their file paths rather than
imported. They are workspace members the root project does not depend on, so
`notebook_host` / `report_host` are only importable after
`uv sync --all-packages`; reading the files directly makes the page generate
from a plain checkout too, and yields the identical model either way.

Run: uv run python scripts/generate_config_reference.py [--check]
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import sys
import textwrap
import types
import typing
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
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_PATH = REPO_ROOT / "docs" / "configuration.md"
CORE_CONFIG_PATH = "packages/core/daimon/core/config.py"
SCHEDULER_CONFIG_PATH = "packages/adapters/scheduler/daimon/adapters/scheduler/settings.py"

# Descriptions are prose, not markdown, and two of them carry a
# `<PLACEHOLDER>` the renderer would otherwise try to read as an HTML tag.
MARKDOWN_ESCAPES = {"<": "&lt;", ">": "&gt;"}

WRAP_WIDTH = 88

# The flat Stripe vars carry no model and therefore no field descriptions.
# These restate what load_billing_config does with each one.
BILLING_VAR_NOTES: dict[str, str] = {
    "STRIPE_SECRET_KEY": "Stripe API secret key used for checkout sessions.",
    "STRIPE_WEBHOOK_SECRET": "Signing secret for the Stripe webhook endpoint.",
    "STRIPE_PRICE_10_USD": "Stripe price id for the $10 top-up.",
    "STRIPE_PRICE_25_USD": "Stripe price id for the $25 top-up.",
    "STRIPE_PRICE_50_USD": "Stripe price id for the $50 top-up.",
    "STRIPE_PRICE_100_USD": "Stripe price id for the $100 top-up.",
    "MCP_PUBLIC_URL": (
        "Origin the checkout return URLs are built from — billing appends "
        "`/billing/success` and `/billing/cancel` to it. Separate from "
        "`DAIMON_MCP__PUBLIC_URL`, which billing does not read."
    ),
}

# docker-compose.yml interpolates these itself; no daimon process reads them.
COMPOSE_VARS: tuple[tuple[str, str, str], ...] = (
    ("POSTGRES_USER", "daimon", "Postgres superuser the `postgres` service is created with."),
    (
        "POSTGRES_PASSWORD",
        "",
        "Its password. Required — every service interpolates it into "
        "`DAIMON_DATABASE__URL` behind a fail-fast `${VAR:?}` guard. Keep it URL-safe "
        "(avoid `@ : / % #`): it is substituted raw into the asyncpg DSN.",
    ),
    ("POSTGRES_DB", "daimon", "Database created on first boot."),
    ("POSTGRES_PORT", "5432", "Host port the container's 5432 is published on, on 127.0.0.1."),
)


@dataclass(frozen=True)
class Section:
    """One rendered `##` section: a heading, an intro, and its variables."""

    title: str
    anchor: str
    intro: list[str]
    body: list[str]


def _escape(text: str) -> str:
    for raw, escaped in MARKDOWN_ESCAPES.items():
        text = text.replace(raw, escaped)
    return text


def _wrap(text: str) -> list[str]:
    return textwrap.wrap(_escape(" ".join(text.split())), width=WRAP_WIDTH) or [""]


def _anchor(title: str) -> str:
    """The heading anchor GitHub derives from a `##` line: lowercased, spaces
    to hyphens, other punctuation dropped."""
    kept = [c for c in title.lower() if c.isalnum() or c in " -_"]
    return "".join(kept).replace(" ", "-")


def _load_module_from_path(module_name: str, relative_path: str) -> types.ModuleType:
    """Import a settings module by file path (see the module docstring)."""
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: pydantic resolves the module's postponed
    # annotations (`from __future__ import annotations`) through sys.modules,
    # and a self-referencing validator return type (`-> Settings`) fails to
    # build without it.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _render_type(annotation: object) -> str:
    """A readable one-line spelling of a field's annotation."""
    if annotation is types.NoneType:
        return "None"
    origin = typing.get_origin(annotation)
    if origin is None:
        name = getattr(annotation, "__name__", None)
        return name if isinstance(name, str) else str(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        return " | ".join(repr(arg) for arg in args)
    if origin in (types.UnionType, typing.Union):
        return " | ".join(_render_type(arg) for arg in args)
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        return f"tuple[{_render_type(args[0])}, ...]"
    origin_name = getattr(origin, "__name__", str(origin))
    return f"{origin_name}[{', '.join(_render_type(arg) for arg in args)}]"


def _render_default(leaf: SettingsLeaf, *, is_secret: bool) -> str:
    """The default an operator gets when they leave the variable unset.

    Never the evaluated value of an env-dependent default_factory — that
    would make --check depend on the machine the generator ran on — and
    never a secret.
    """
    if leaf.env_name in ENV_DEPENDENT_PLACEHOLDERS:
        return "read from the process environment"
    field = leaf.field
    if field.is_required():
        return ""
    if is_secret:
        return "unset"
    if field.default_factory is not None:
        # Every factory reaching this point is a pure constructor
        # (`lambda: Path("defaults")`, `list[str]`); the env-reading one is
        # handled by the placeholder branch above.
        value = field.default_factory()  # pyright: ignore[reportCallIssue]
    elif field.default is PydanticUndefined:
        return "unset"
    else:
        value = field.default
    if value is None or value == "" or value == [] or value == ():
        return "unset"
    return f"`{stringify_default(value)}`"


def _render_leaf(leaf: SettingsLeaf) -> list[str]:
    is_secret = is_secret_annotation(leaf.field.annotation)
    facts = [f"`{_escape(_render_type(leaf.field.annotation))}`"]
    if leaf.field.is_required():
        facts.append("**required**")
    elif leaf.env_name in ADAPTER_REQUIRED_NOTES:
        facts.append(f"**{ADAPTER_REQUIRED_NOTES[leaf.env_name]}**")
    else:
        facts.append("optional")
    default = _render_default(leaf, is_secret=is_secret)
    if default:
        facts.append(f"default {default}")
    if is_secret:
        facts.append("secret")

    lines = [f"### `{leaf.env_name}`", "", " · ".join(facts)]
    if leaf.field.description:
        lines.extend(["", *_wrap(leaf.field.description)])
    return lines


def _own_docstring(model: type[BaseModel]) -> str | None:
    """The model's own class docstring — never an inherited one, or every
    block without a docstring would render pydantic's BaseModel docs."""
    doc = model.__dict__.get("__doc__")
    return textwrap.dedent(doc).strip() if isinstance(doc, str) and doc.strip() else None


def _model_intro(model: type[BaseModel], qualified_name: str, prefix: str) -> list[str]:
    lines = _wrap(f"Read from `{qualified_name}`. Prefix `{prefix}`.")
    docstring = _own_docstring(model)
    if docstring is not None:
        lines.append("")
        for index, paragraph in enumerate(docstring.split("\n\n")):
            if index:
                lines.append("")
            lines.extend(_wrap(paragraph))
    return lines


def _leaf_section(
    *,
    title: str,
    intro: list[str],
    leaves: typing.Sequence[SettingsLeaf],
    source_path: str,
) -> Section:
    if leaves and not any(leaf.field.description for leaf in leaves):
        # Silence here would read as "these need no explanation". Say which
        # file to read instead, and make the gap visible enough to fix.
        intro = [
            *intro,
            "",
            *_wrap(
                "No field in this model carries a `Field(description=...)`, so this "
                f"section lists types and defaults only. `{source_path}` documents "
                "them in inline comments."
            ),
        ]
    body: list[str] = []
    for leaf in leaves:
        body.extend(_render_leaf(leaf))
        body.append("")
    return Section(title=title, anchor=_anchor(title), intro=intro, body=body[:-1] if body else [])


def _core_sections() -> list[Section]:
    core_leaves, blocks = split_top_level(Settings, "DAIMON_")
    sections = [
        _leaf_section(
            title="Core",
            intro=_wrap(
                "Read from `daimon.core.config.Settings`. Prefix `DAIMON_`. Every other "
                "`DAIMON_*` section below is a nested block on this model, reached with "
                "the `__` delimiter."
            ),
            leaves=core_leaves,
            source_path=CORE_CONFIG_PATH,
        )
    ]
    for block in blocks:
        intro = _model_intro(
            block.model,
            f"daimon.core.config.{block.model.__name__}",
            block.env_prefix,
        )
        if block.is_absent_by_default:
            intro.extend(
                [
                    "",
                    *_wrap(
                        "This whole block is optional: it stays unset until at least one "
                        "of its variables is set, and the features that read it are "
                        "inactive while it is."
                    ),
                ]
            )
        if block.field.description:
            intro.extend(["", *_wrap(block.field.description)])
        sections.append(
            _leaf_section(
                title=section_title(block.field_name),
                intro=intro,
                leaves=block.leaves,
                source_path=CORE_CONFIG_PATH,
            )
        )
    return sections


def _scheduler_section() -> Section:
    return _leaf_section(
        title="Scheduler",
        intro=_model_intro(
            SchedulerSettings,
            "daimon.adapters.scheduler.settings.SchedulerSettings",
            "DAIMON_SCHEDULER__",
        ),
        leaves=collect_leaves(SchedulerSettings, "DAIMON_SCHEDULER__"),
        source_path=SCHEDULER_CONFIG_PATH,
    )


def _app_section(
    *,
    title: str,
    module_name: str,
    relative_path: str,
    dotted: str,
    already_documented: typing.AbstractSet[str],
) -> Section:
    module = _load_module_from_path(module_name, relative_path)
    model: type[BaseModel] = module.Settings
    prefix = str(model.model_config.get("env_prefix", ""))  # pyright: ignore[reportAttributeAccessIssue]
    leaves = collect_leaves(model, prefix)
    intro = _model_intro(model, dotted, prefix)
    intro.extend(
        [
            "",
            *_wrap(
                f"A standalone service in `{Path(relative_path).parents[2]}`, deployed and "
                "configured separately from the daimon processes. It is not part of "
                "`docker-compose.yml`."
            ),
        ]
    )
    shared = sorted(leaf.env_name for leaf in leaves if leaf.env_name in already_documented)
    if shared:
        names = ", ".join(f"`{name}`" for name in shared)
        intro.extend(
            [
                "",
                *_wrap(
                    f"This service shares the `{prefix}` prefix with a block on daimon's "
                    f"own Settings, so {names} appear twice on this page — once for the "
                    "service and once for the daimon side that calls it. They are read "
                    "by different processes; a single shared env file would set both."
                ),
            ]
        )
    return _leaf_section(title=title, intro=intro, leaves=leaves, source_path=relative_path)


def _billing_section() -> Section:
    intro = _wrap(
        "Read from the process environment by "
        "`daimon.core.billing.load_billing_config`, not from a settings model — "
        "these carry no `DAIMON_` prefix. All seven are required together: with "
        "any one unset, billing is disabled rather than rejected, and the "
        "top-up flow cannot create a checkout session."
    )
    body: list[str] = []
    for name in BILLING_FLAT_VARS:
        body.extend(
            [
                f"### `{name}`",
                "",
                "`str` · required for billing"
                + (" · secret" if name.endswith(("SECRET_KEY", "WEBHOOK_SECRET")) else ""),
                "",
                *_wrap(BILLING_VAR_NOTES[name]),
                "",
            ]
        )
    return Section(
        title="Billing (Stripe)", anchor=_anchor("Billing (Stripe)"), intro=intro, body=body[:-1]
    )


def _compose_section() -> Section:
    intro = _wrap(
        "Interpolated by `docker-compose.yml` itself; no daimon process reads "
        "them. They exist so the compose file can build "
        "`DAIMON_DATABASE__URL` for every service from one password."
    )
    body: list[str] = []
    for name, default, note in COMPOSE_VARS:
        facts = f"`str` · optional · default `{default}`" if default else "`str` · **required**"
        body.extend([f"### `{name}`", "", facts, "", *_wrap(note), ""])
    return Section(
        title="Docker Compose", anchor=_anchor("Docker Compose"), intro=intro, body=body[:-1]
    )


def _header(sections: typing.Sequence[Section]) -> list[str]:
    lines = [
        "# Configuration reference",
        "",
        *_wrap(
            "Every environment variable daimon reads. Generated from the settings "
            "models themselves by `scripts/generate_config_reference.py` — edit the "
            "`Field(description=...)` in the model, not this page. CI fails when the "
            "two disagree."
        ),
        "",
        *_wrap(
            "Values come from the process environment and, for the daimon processes, "
            "from a `.env` file in the working directory. `.env.example` lists the "
            "same `DAIMON_*` variables in copy-paste form; this page adds the types, "
            "the defaults and the two standalone services. Nested blocks use `__` as "
            "the delimiter, so `DAIMON_MCP__JWT_SECRET` is `Settings.mcp.jwt_secret`."
        ),
        "",
        *_wrap(
            "Unknown `DAIMON_*` variables are ignored rather than rejected "
            '(`extra="ignore"`), so a typo is silent — check the spelling here.'
        ),
        "",
        "## Contents",
        "",
    ]
    lines.extend(f"- [{section.title}](#{section.anchor})" for section in sections)
    return lines


def render_reference() -> str:
    sections = [*_core_sections(), _scheduler_section()]
    documented = {
        line.removeprefix("### `").removesuffix("`")
        for section in sections
        for line in section.body
        if line.startswith("### `")
    }
    sections.extend(
        [
            _app_section(
                title="Notebook host (standalone service)",
                module_name="notebook_host.config",
                relative_path="apps/notebook-host/src/notebook_host/config.py",
                dotted="notebook_host.config.Settings",
                already_documented=documented,
            ),
            _app_section(
                title="Report host (standalone service)",
                module_name="report_host.config",
                relative_path="apps/report-host/src/report_host/config.py",
                dotted="report_host.config.Settings",
                already_documented=documented,
            ),
            _billing_section(),
            _compose_section(),
        ]
    )

    lines = _header(sections)
    for section in sections:
        lines.extend(["", f"## {section.title}", "", *section.intro, ""])
        lines.extend(section.body)
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/configuration.md from the settings models.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed page matches generator output; exit nonzero on drift.",
    )
    args = parser.parse_args(argv)

    generated = render_reference()

    if args.check:
        current = REFERENCE_PATH.read_text() if REFERENCE_PATH.exists() else ""
        if current != generated:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                generated.splitlines(keepends=True),
                fromfile="docs/configuration.md (committed)",
                tofile="docs/configuration.md (generated)",
            )
            sys.stderr.writelines(diff)
            print(
                "\ndocs/configuration.md is out of date. "
                "Run: uv run python scripts/generate_config_reference.py",
                file=sys.stderr,
            )
            return 1
        return 0

    REFERENCE_PATH.write_text(generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())

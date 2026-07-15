"""Walk `defaults/` and parse each YAML / SKILL.md into a `*Spec`.

Authoring invariants enforced here (before any MA/DB write):
- `defaults/agents/<name>.yaml` must set `name: <name>` matching the filename.
- `defaults/environments/<name>.yaml` — same.
- `defaults/skills/<name>/SKILL.md` must set frontmatter `name: <name>`
  matching the local directory.

Failures surface as `DefaultsError` with a file-pointing message so operators
can jump straight to the broken spec.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import yaml
from daimon.core.errors import DefaultsError
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec, EnvironmentSpec, SkillSpec, SystemConfigSpec
from pydantic import ValidationError

_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)$", re.DOTALL)


def _load_yaml(path: Path) -> dict[str, object]:
    try:
        loaded = yaml.safe_load(path.read_text())
    except yaml.YAMLError as err:
        raise DefaultsError(f"{path}: YAML parse error: {err}") from err
    if not isinstance(loaded, dict):
        raise DefaultsError(f"{path}: top-level must be a mapping, got {type(loaded).__name__}")
    return cast(dict[str, object], loaded)


def load_agent_specs(root: Path) -> list[AgentSpec]:
    specs: list[AgentSpec] = []
    for path in sorted(root.glob("*.yaml")):
        data = _load_yaml(path)
        try:
            spec = AgentSpec.model_validate(data)
        except ValidationError as err:
            raise DefaultsError(f"{path}: {err}") from err
        if spec.name != path.stem:
            raise DefaultsError(
                f"{path}: filename stem {path.stem!r} must equal name {spec.name!r}"
            )
        specs.append(spec)
    return specs


def load_environment_specs(root: Path) -> list[EnvironmentSpec]:
    specs: list[EnvironmentSpec] = []
    for path in sorted(root.glob("*.yaml")):
        data = _load_yaml(path)
        try:
            spec = EnvironmentSpec.model_validate(data)
        except ValidationError as err:
            raise DefaultsError(f"{path}: {err}") from err
        if spec.name != path.stem:
            raise DefaultsError(
                f"{path}: filename stem {path.stem!r} must equal name {spec.name!r}"
            )
        specs.append(spec)
    return specs


def load_skill_paths(root: Path) -> list[Path]:
    """Return the list of skill directory paths under `root`.

    A skill is any direct child directory containing `SKILL.md`. Other
    children are ignored (with no warning — `README.md`, etc. are harmless).
    """
    if not root.exists():
        return []
    paths: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            paths.append(entry)
    return paths


RESERVED_SKILL_SUBSTRINGS: tuple[str, ...] = ("anthropic", "claude")


def _assert_skill_name_consistent(skill_dir: Path, spec: SkillSpec, manifest: Path) -> None:
    if spec.name != skill_dir.name:
        raise DefaultsError(
            f"{manifest}: dir name {skill_dir.name!r} must equal frontmatter name {spec.name!r}"
        )
    lowered = spec.name.lower()
    for reserved in RESERVED_SKILL_SUBSTRINGS:
        if reserved in lowered:
            raise DefaultsError(
                f"{manifest}: skill name {spec.name!r} contains reserved "
                f"substring {reserved!r}; MA rejects skill names containing "
                f"'anthropic' or 'claude'. Rename the skill."
            )


def parse_skill_frontmatter(path: Path) -> tuple[SkillSpec, str]:
    """Parse SKILL.md frontmatter from any path; no directory-name enforcement.

    Returns (SkillSpec, body_markdown). Raises DefaultsError on missing/invalid
    frontmatter.
    """
    content = path.read_text()
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        raise DefaultsError(f"{path}: missing YAML frontmatter (expected leading '---')")
    try:
        frontmatter = yaml.safe_load(match.group("frontmatter"))
    except yaml.YAMLError as err:
        raise DefaultsError(f"{path}: frontmatter YAML parse error: {err}") from err
    if not isinstance(frontmatter, dict):
        raise DefaultsError(f"{path}: frontmatter must be a mapping")
    try:
        spec = SkillSpec.model_validate(frontmatter)
    except ValidationError as err:
        raise DefaultsError(f"{path}: {err}") from err
    return spec, match.group("body")


def load_skill_spec(skill_dir: Path) -> tuple[SkillSpec, str]:
    """Parse `SKILL.md` in `skill_dir`; return `(SkillSpec, body_markdown)`.

    `SkillSpec` allows extra frontmatter keys (skills-design §5.2); they're
    preserved in the model dump but daimon does not interpret them.
    """
    manifest = skill_dir / "SKILL.md"
    spec, body = parse_skill_frontmatter(manifest)
    _assert_skill_name_consistent(skill_dir, spec, manifest)
    return spec, body


def load_system_config(defaults_root: Path) -> SystemConfigSpec | None:
    """Parse `defaults_root/config.yaml` into a `SystemConfigSpec`.

    Returns `None` when the file is absent (a deployment may omit the file to
    keep `--agent` / `--environment` mandatory). Malformed YAML or unknown
    fields raise `DefaultsError` with a file-pointing message, matching the
    other loaders in this module.
    """
    path = defaults_root / "config.yaml"
    if not path.exists():
        return None
    data = _load_yaml(path)
    try:
        return SystemConfigSpec.model_validate(data)
    except ValidationError as err:
        raise DefaultsError(f"{path}: {err}") from err


def parse_deployment_default(defaults_root: Path) -> DeploymentDefault:
    """Parse `defaults_root/config.yaml` into a `DeploymentDefault`.

    Returns `DeploymentDefault()` (both fields `None`) when `config.yaml` is
    absent — callers can always inject the result without a None check.
    Malformed YAML or unknown fields propagate as `DefaultsError`.
    """
    spec = load_system_config(defaults_root)
    if spec is None:
        return DeploymentDefault()
    return DeploymentDefault(
        agent_name=spec.agent_name,
        environment_name=spec.environment_name,
    )

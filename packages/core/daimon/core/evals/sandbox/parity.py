"""Does the sandbox image carry the production environment definition's packages?"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from daimon.core.specs import EnvironmentSpec

_NAME = re.compile(r"^[A-Za-z0-9._-]+")


def _name(requirement: str) -> str:
    match = _NAME.match(requirement.strip())
    return (match.group(0) if match else requirement).lower().replace("_", "-")


def missing_packages(spec: EnvironmentSpec, manifest: Mapping[str, Sequence[str]]) -> list[str]:
    """Packages the definition installs that the image manifest lacks, as `manager:name`."""
    packages = cast(Mapping[str, object], (spec.config or {}).get("packages") or {})
    missing: list[str] = []
    for manager in ("apt", "pip", "npm", "cargo", "gem", "go"):
        wanted = [str(p) for p in cast(Sequence[object], packages.get(manager) or [])]
        have = {_name(p) for p in manifest.get(manager, [])}
        missing += [f"{manager}:{_name(p)}" for p in wanted if _name(p) not in have]
    return missing


def parity(
    spec: EnvironmentSpec, manifest: Mapping[str, Sequence[str]]
) -> Literal["matched", "drift"]:
    return "drift" if missing_packages(spec, manifest) else "matched"

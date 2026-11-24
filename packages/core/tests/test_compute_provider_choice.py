"""One place chooses the compute provider, and only a provider reads its own settings.

The MCP service launches sessions and the scheduler cleans them up, as separate
processes. A process that built its own provider would look for sessions in the
wrong cloud: launches would fail, or paid machines would never be deleted. The
contract tests read the source of every package, so a new process or a new check
that names one provider fails here until it goes through `compute_provider_for`
and `ComputeSettings.provider_configured`.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path
from typing import get_args

import pytest
from daimon.core import compute_provider
from daimon.core.compute_cloud_run import CloudRunComputeProvider
from daimon.core.compute_fly import FlyComputeProvider
from daimon.core.compute_models import ComputeResource
from daimon.core.compute_provider import (
    ComputeProviderError,
    app_name,
    compute_provider_for,
    region_provider,
)
from daimon.core.config import ComputeProfile, ComputeSettings
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import server_setup_met
from daimon.testing.shared_helpers import settings as base_settings
from pydantic import SecretStr, ValidationError

_PACKAGES = Path(__file__).resolve().parents[2]
_CORE = Path(compute_provider.__file__).parent
_CLOUD_RUN = {
    "provider": "cloud_run",
    "cloud_run_project": "test-project",
    "cloud_run_service_account": "session@test-project.iam.gserviceaccount.com",
}
_FLY = {"api_token": SecretStr("fly-test-token"), "org_slug": "org"}
PROVIDERS = {"fly": FlyComputeProvider, "cloud_run": CloudRunComputeProvider}


def _production_sources() -> list[Path]:
    """Every shipped module: tests and the shared test helpers build fakes on purpose."""
    return [
        path
        for path in _PACKAGES.rglob("*.py")
        if "tests" not in path.parts
        and ".venv" not in path.parts
        and _PACKAGES / "testing" not in path.parents
    ]


def _provider_modules() -> dict[str, str]:
    """`module name -> class name` for every core module that defines a provider class."""
    found: dict[str, str] = {}
    for path in _CORE.glob("compute_*.py"):
        for node in ast.parse(path.read_text()).body:
            if (
                isinstance(node, ast.ClassDef)
                and node.name.endswith("ComputeProvider")
                and node.name != "ComputeProvider"
            ):
                found[f"daimon.core.{path.stem}"] = node.name
    return found


def test_the_setting_selects_every_provider_and_fly_stays_the_default():
    values = get_args(ComputeSettings.model_fields["provider"].annotation)
    assert set(values) == set(PROVIDERS), "a provider value has no provider class in this test"
    assert type(compute_provider_for(ComputeSettings())) is FlyComputeProvider
    for value, expected in PROVIDERS.items():
        settings = ComputeSettings(**(_CLOUD_RUN if value == "cloud_run" else {"provider": value}))
        assert type(compute_provider_for(settings)) is expected
    assert {cls.__name__ for cls in PROVIDERS.values()} == set(_provider_modules().values()), (
        "a provider class exists that DAIMON_COMPUTE__PROVIDER cannot select"
    )


def _imported_modules(source: str, module: str, *, package: bool = False) -> set[str]:
    """Every dotted name `source` imports, with relative imports resolved against `module`.

    `from x import y` yields both `x` and `x.y`, because `y` may be a module.
    """
    parts = module.split(".") if package else module.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = parts[: len(parts) - node.level + 1] if node.level else []
            origin = ".".join([*base, *([node.module] if node.module else [])])
            found.add(origin)
            found.update(f"{origin}.{alias.name}" for alias in node.names)
    return found


def _module_name(path: Path) -> tuple[str, bool]:
    """The dotted name of a file under `packages/`, and whether it is a package."""
    parts = path.with_suffix("").parts
    dotted = list(parts[parts.index("daimon") :] if "daimon" in parts else parts[-2:])
    package = dotted[-1] == "__init__"
    return ".".join(dotted[:-1] if package else dotted), package


@pytest.mark.parametrize(
    ("source", "module", "caught"),
    [
        ("from daimon.core.compute_fly import FlyComputeProvider", "daimon.adapters.x.main", True),
        ("import daimon.core.compute_fly", "daimon.adapters.x.main", True),
        ("import daimon.core.compute_fly as fly", "daimon.adapters.x.main", True),
        ("from daimon.core import compute_fly", "daimon.adapters.x.main", True),
        # Relative forms, from a sibling, from the package and from a subpackage.
        ("from .compute_fly import FlyComputeProvider", "daimon.core.compute_sessions", True),
        ("from . import compute_fly", "daimon.core.compute_sessions", True),
        ("from ..compute_fly import FlyComputeProvider", "daimon.core.stores.compute", True),
        ("from .. import compute_fly", "daimon.core.stores.compute", True),
        ("from ..core.compute_fly import FlyComputeProvider", "daimon.core.compute_tasks", True),
        ("def late():\n    from .compute_fly import FlyComputeProvider", "daimon.core.x", True),
        ("from .compute_provider import compute_provider_for", "daimon.core.x", False),
        ("from .compute_fly import FlyComputeProvider", "daimon.adapters.mcp.tools.fly", False),
        ("from daimon.core.compute_models import ComputeSession", "daimon.core.x", False),
    ],
)
def test_a_provider_import_is_found_in_every_form(source, module, caught):
    assert ("daimon.core.compute_fly" in _imported_modules(source, module)) is caught


def test_a_relative_import_from_a_package_file_resolves_to_that_package():
    found = _imported_modules("from . import compute_fly", "daimon.core", package=True)
    assert "daimon.core.compute_fly" in found


def test_only_the_factory_imports_a_provider_module():
    modules = _provider_modules()
    assert len(modules) >= 2
    offenders: list[str] = []
    stems = [module.rsplit(".", 1)[1] for module in modules]
    for path in _production_sources():
        source = path.read_text()
        # Parse only a file that names a provider module at all.
        if path == _CORE / "compute_provider.py" or not any(stem in source for stem in stems):
            continue
        name, package = _module_name(path)
        offenders += [
            f"{path.relative_to(_PACKAGES)} imports {imported}"
            for imported in sorted(_imported_modules(source, name, package=package) & set(modules))
        ]
    assert not offenders, "build the provider with compute_provider_for(settings): " + ", ".join(
        offenders
    )


def test_only_a_provider_reads_its_own_settings():
    """A check such as `compute.api_token is None` silently turns the other provider off."""
    fields = "|".join(
        name
        for name in ComputeSettings.model_fields
        if name in {"api_token", "org_slug"} or name.startswith("cloud_run_")
    )
    read = re.compile(rf"\bcompute(?:\.settings)?\.(?:{fields})\b")
    exempt = {_CORE / "config.py"} | {
        _CORE / (module.rsplit(".", 1)[1] + ".py") for module in _provider_modules()
    }
    offenders = [
        f"{path.relative_to(_PACKAGES)}:{number}"
        for path in _production_sources()
        if path not in exempt
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if read.search(line)
    ]
    assert not offenders, "ask ComputeSettings.provider_configured instead: " + ", ".join(offenders)


@pytest.mark.parametrize(
    ("settings", "configured"),
    [
        ({}, False),
        ({"api_token": SecretStr("t")}, False),
        (_FLY, True),
        (_CLOUD_RUN, True),
        # The other provider's leftovers never count for the selected one.
        ({**_FLY, "provider": "fly", "cloud_run_project": "p"}, True),
    ],
)
def test_setup_is_judged_for_the_selected_provider(settings, configured):
    compute = ComputeSettings(enabled=True, deployment_id="d", **settings)
    assert compute.provider_configured is configured
    # The same rule decides whether a conversation is offered the compute tools.
    settings = base_settings().model_copy(update={"compute": compute})
    assert server_setup_met(FEATURES["compute"], settings) is configured


def test_cloud_run_without_its_settings_is_refused_when_settings_load():
    with pytest.raises(ValidationError) as error:
        ComputeSettings(provider="cloud_run")
    message = str(error.value)
    assert "DAIMON_COMPUTE__CLOUD_RUN_PROJECT" in message
    assert "DAIMON_COMPUTE__CLOUD_RUN_SERVICE_ACCOUNT" in message
    with pytest.raises(ValidationError, match="DAIMON_COMPUTE__CLOUD_RUN_SERVICE_ACCOUNT"):
        ComputeSettings(provider="cloud_run", cloud_run_project="p")


@pytest.mark.parametrize(
    ("profile", "problem"),
    [
        ({}, "Google Cloud regions"),  # the default region is Fly's iad
        ({"regions": []}, "Google Cloud regions"),
        ({"regions": ["us-central1", "iad"]}, "Google Cloud regions"),
        ({"regions": ["us-central1"], "cpus": 3}, "1, 2, 4, 6 or 8 CPUs"),
        ({"regions": ["us-central1"], "cpus": 16, "memory_mb": 32768}, "1, 2, 4, 6 or 8 CPUs"),
        ({"regions": ["us-central1"], "cpus": 1, "memory_mb": 8192}, "512 to 4096 MB"),
        ({"regions": ["us-central1"], "cpus": 2, "memory_mb": 256}, "512 to 8192 MB"),
        ({"regions": ["us-central1"], "cpus": 4, "memory_mb": 1024}, "2048 to 16384 MB"),
        ({"regions": ["us-central1"], "cpus": 6, "memory_mb": 32768}, "4096 to 24576 MB"),
        ({"regions": ["us-central1"], "cpus": 8, "memory_mb": 65536}, "4096 to 32768 MB"),
    ],
)
def test_a_profile_cloud_run_would_refuse_fails_when_settings_load(profile, problem):
    with pytest.raises(ValidationError, match=problem):
        ComputeSettings(**_CLOUD_RUN, profiles={"mmm": ComputeProfile(image="i", **profile)})
    # Fly keeps accepting everything it accepted before.
    ComputeSettings(profiles={"mmm": ComputeProfile(image="i", **profile)})


@pytest.mark.parametrize(
    "profile",
    [
        {"regions": ["us-central1"]},
        {"regions": ["europe-west4", "northamerica-northeast1"], "cpus": 1, "memory_mb": 512},
        {"regions": ["me-central2"], "cpus": 8, "memory_mb": 32768},
        {"regions": ["asia-southeast1"], "cpus": 6, "memory_mb": 4096},
    ],
)
def test_cloud_run_accepts_the_profiles_it_can_run(profile):
    ComputeSettings(**_CLOUD_RUN, profiles={"mmm": ComputeProfile(image="i", **profile)})


@pytest.mark.parametrize(
    ("settings", "other_region"),
    [
        ({**_FLY, "session_auth_key": SecretStr("k")}, "us-central1"),
        (_CLOUD_RUN, "iad"),
    ],
)
async def test_a_provider_refuses_the_other_providers_sessions(settings, other_region):
    """After the provider setting changes, a missing resource must not read as deleted."""
    assert region_provider(other_region) != ComputeSettings(**settings).provider
    provider = compute_provider_for(ComputeSettings(deployment_id="deployment-a", **settings))
    session_id = uuid.uuid4()
    with pytest.raises(ComputeProviderError, match="another cloud provider"):
        # Refused before any request: no transport is configured to answer one.
        await provider.delete(
            ComputeResource(
                app_name=app_name("deployment-a", session_id),
                deployment_id="deployment-a",
                session_id=session_id,
                region=other_region,
            )
        )

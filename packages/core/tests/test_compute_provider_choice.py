"""One place builds the compute provider, and only the provider reads its own settings.

The MCP service launches sessions and the scheduler cleans them up, as separate
processes. A process that built its own provider could look for sessions somewhere
else: launches would fail, or paid machines would never be deleted. The contract
tests read the source of every package, so a new process or a new check that names
a provider fails here until it goes through `compute_provider_for` and
`ComputeSettings.provider_configured`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from daimon.core import compute_provider
from daimon.core.compute_cloud_run import CloudRunComputeProvider
from daimon.core.compute_provider import compute_provider_for
from daimon.core.config import ComputeProfile, ComputeSettings, load_settings
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import server_setup_met
from daimon.testing.shared_helpers import settings as base_settings
from pydantic import ValidationError

_PACKAGES = Path(__file__).resolve().parents[2]
_CORE = Path(compute_provider.__file__).parent
_CLOUD_RUN = {
    "cloud_run_project": "test-project",
    "cloud_run_service_account": "session@test-project.iam.gserviceaccount.com",
}
_PROVIDER = "daimon.core.compute_cloud_run"


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


def test_the_factory_builds_every_provider_class():
    built = type(compute_provider_for(ComputeSettings(**_CLOUD_RUN)))
    assert built is CloudRunComputeProvider
    assert set(_provider_modules().values()) == {built.__name__}, (
        "a provider class exists that compute_provider_for cannot build"
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
        (
            "from daimon.core.compute_cloud_run import CloudRunComputeProvider",
            "daimon.adapters.x.main",
            True,
        ),
        ("import daimon.core.compute_cloud_run", "daimon.adapters.x.main", True),
        ("import daimon.core.compute_cloud_run as fly", "daimon.adapters.x.main", True),
        ("from daimon.core import compute_cloud_run", "daimon.adapters.x.main", True),
        # Relative forms, from a sibling, from the package and from a subpackage.
        (
            "from .compute_cloud_run import CloudRunComputeProvider",
            "daimon.core.compute_sessions",
            True,
        ),
        ("from . import compute_cloud_run", "daimon.core.compute_sessions", True),
        (
            "from ..compute_cloud_run import CloudRunComputeProvider",
            "daimon.core.stores.compute",
            True,
        ),
        ("from .. import compute_cloud_run", "daimon.core.stores.compute", True),
        (
            "from ..core.compute_cloud_run import CloudRunComputeProvider",
            "daimon.core.compute_tasks",
            True,
        ),
        (
            "def late():\n    from .compute_cloud_run import CloudRunComputeProvider",
            "daimon.core.x",
            True,
        ),
        ("from .compute_provider import compute_provider_for", "daimon.core.x", False),
        (
            "from .compute_cloud_run import CloudRunComputeProvider",
            "daimon.adapters.mcp.tools.x",
            False,
        ),
        ("from daimon.core.compute_models import ComputeSession", "daimon.core.x", False),
    ],
)
def test_a_provider_import_is_found_in_every_form(source, module, caught):
    assert (_PROVIDER in _imported_modules(source, module)) is caught


def test_a_relative_import_from_a_package_file_resolves_to_that_package():
    found = _imported_modules("from . import compute_cloud_run", "daimon.core", package=True)
    assert _PROVIDER in found


def test_only_the_factory_imports_a_provider_module():
    modules = _provider_modules()
    assert _PROVIDER in modules
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
    """A check on one provider's setting, made outside it, silently breaks the next provider."""
    fields = "|".join(
        name for name in ComputeSettings.model_fields if name.startswith("cloud_run_")
    )
    assert fields
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


@pytest.mark.parametrize(("settings", "configured"), [({}, False), (_CLOUD_RUN, True)])
def test_setup_is_judged_by_the_provider_settings(settings, configured):
    compute = ComputeSettings(enabled=True, deployment_id="d", **settings)
    assert compute.provider_configured is configured
    # The same rule decides whether a conversation is offered the compute tools.
    offered = base_settings().model_copy(update={"compute": compute})
    assert server_setup_met(FEATURES["compute"], offered) is configured


@pytest.mark.parametrize(("given", "missing"), [(0, 1), (1, 0)])
def test_a_half_configured_provider_is_refused_when_settings_load(given, missing):
    fields = list(_CLOUD_RUN)
    with pytest.raises(ValidationError, match=f"DAIMON_COMPUTE__{fields[missing].upper()}"):
        ComputeSettings(**{fields[given]: _CLOUD_RUN[fields[given]]})


_REFUSED_PROFILES = [
    ({}, "Google Cloud regions"),
    ({"regions": ["us-central1", "iad"]}, "Google Cloud regions"),
    ({"regions": ["us-central1"], "cpus": 3}, "1, 2, 4, 6 or 8 CPUs"),
    ({"regions": ["us-central1"], "cpus": 16, "memory_mb": 32768}, "1, 2, 4, 6 or 8 CPUs"),
    ({"regions": ["us-central1"], "cpus": 1, "memory_mb": 8192}, "512 to 4096 MB"),
    ({"regions": ["us-central1"], "cpus": 2, "memory_mb": 256}, "512 to 8192 MB"),
    ({"regions": ["us-central1"], "cpus": 4, "memory_mb": 1024}, "2048 to 16384 MB"),
    ({"regions": ["us-central1"], "cpus": 6, "memory_mb": 32768}, "4096 to 24576 MB"),
    ({"regions": ["us-central1"], "cpus": 8, "memory_mb": 65536}, "4096 to 32768 MB"),
]


@pytest.mark.parametrize(("profile", "problem"), _REFUSED_PROFILES)
def test_a_profile_cloud_run_would_refuse_fails_when_settings_load(profile, problem):
    with pytest.raises(ValidationError, match=problem):
        ComputeSettings(**_CLOUD_RUN, profiles={"mmm": ComputeProfile(image="i", **profile)})


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


@pytest.mark.parametrize("length", [1000, 1001])
def test_a_profile_description_over_1000_characters_stops_startup(monkeypatch, length):
    """The agent reads it in every listing and launch, so a longer one is refused at start."""
    import json

    profile = {"image": "i", "regions": ["us-central1"], "description": "x" * length}
    for name, value in {
        "DATABASE__URL": "postgresql+asyncpg://u:p@h:5432/d",
        "ANTHROPIC__API_KEY": "sk-test",
        "COMPUTE__PROFILES": json.dumps({"mmm": profile}),
    }.items():
        monkeypatch.setenv("DAIMON_" + name, value)
    if length == 1000:
        assert load_settings(_env_file=None).compute.profiles["mmm"].description == "x" * 1000
        return
    with pytest.raises(
        ValidationError, match=r"profiles\.mmm\.description\s+String should have at most 1000"
    ):
        load_settings(_env_file=None)


def test_settings_of_the_removed_fly_provider_do_not_stop_startup(monkeypatch):
    """A deployment that still passes Fly settings starts, with compute not set up."""
    for name, value in {
        "DATABASE__URL": "postgresql+asyncpg://u:p@h:5432/d",
        "ANTHROPIC__API_KEY": "sk-test",
        "COMPUTE__ENABLED": "true",
        "COMPUTE__PROVIDER": "fly",
        "COMPUTE__DEPLOYMENT_ID": "deployment-a",
        "COMPUTE__API_TOKEN": "fly-token",
        "COMPUTE__ORG_SLUG": "org",
        "COMPUTE__SESSION_AUTH_KEY": "key",
        "COMPUTE__PROFILES": (
            '{"mmm": {"image": "registry.example/mmm@sha256:abc", "regions": ["iad"],'
            ' "cpu_kind": "shared", "cpus": 2, "memory_mb": 4096}}'
        ),
    }.items():
        monkeypatch.setenv("DAIMON_" + name, value)
    settings = load_settings(_env_file=None)
    assert settings.compute.enabled and not settings.compute.provider_configured
    assert not server_setup_met(FEATURES["compute"], settings)
    for removed in ("provider", "api_token", "org_slug"):
        assert not hasattr(settings.compute, removed)
    assert not hasattr(settings.compute.profiles["mmm"], "cpu_kind")

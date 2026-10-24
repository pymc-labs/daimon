"""Keep QA files out of wheel packages; import policy lives in import-linter."""

from __future__ import annotations

import argparse
import sys
import tomllib
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
TESTING = "daimon-testing"
MUX_TESTING_PREFIX = "mux/testing/"
HARNESS_PARTS = {
    "conformance",
    "backend_conformance",
    "default_capability",
    "offline_sdk",
    "live_cert",
    "fake",
    "round_cases",
    "round_usage",
    "round_runner",
    "certification",
}


def file_violation(member: str, *, testing: bool) -> str | None:
    path = PurePosixPath(member)
    if path.name.startswith("test_") and path.suffix == ".py" or path.name == "conftest.py":
        return "test module inside wheel package"
    if testing or member.startswith(MUX_TESTING_PREFIX):
        # `mux.testing` is the test-double package that ships inside daimon-mux
        # on purpose; import-linter keeps product modules from importing it.
        return None
    if any(
        PurePosixPath(part).stem in HARNESS_PARTS or part.startswith("conformance_")
        for part in path.parts
    ):
        return "QA harness or receipts inside product wheel package"
    if member.startswith("daimon/testing/"):
        return "test support inside product wheel package"
    if path.name == "live-budget.example.json":
        return "probe budget config inside product wheel package"
    if member.startswith("mux/") and path.suffix == ".md":
        return "package documentation inside product wheel package"
    return None


def source_violations(root: Path) -> list[str]:
    failures: list[str] = []
    projects = [*root.glob("packages/**/pyproject.toml"), *root.glob("apps/*/pyproject.toml")]
    for project in sorted(projects):
        config = tomllib.loads(project.read_text())
        testing = config.get("project", {}).get("name") == TESTING
        packages = (
            config.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel", {})
            .get("packages", [])
        )
        for package in packages:
            package_root = project.parent / package
            if not package_root.is_dir():
                failures.append(f"{project.relative_to(root)}: missing wheel package {package}")
                continue
            for path in sorted(package_root.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                member = path.relative_to(package_root.parent).as_posix()
                if message := file_violation(member, testing=testing):
                    failures.append(f"{path.relative_to(root)}: {message}")
    return failures


def wheel_violations(path: Path) -> list[str]:
    failures: list[str] = []
    with ZipFile(path) as wheel:
        metadata = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            return [f"{path.name}: expected one distribution metadata record"]
        name = BytesParser().parsebytes(wheel.read(metadata[0]))["Name"]
        if not name:
            return [f"{path.name}: distribution name missing"]
        for member in sorted(wheel.namelist()):
            if any(part.endswith(".dist-info") for part in PurePosixPath(member).parts):
                continue
            if message := file_violation(member, testing=name == TESTING):
                failures.append(f"{path.name}:{member}: {message}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--wheels", type=Path, help="Directory of built workspace wheels")
    args = parser.parse_args()
    failures = source_violations(args.root)
    if args.wheels is not None:
        wheels = sorted(args.wheels.glob("*.whl"))
        if not wheels:
            failures.append("No wheels found; distribution verification cannot be skipped")
        for wheel in wheels:
            failures.extend(wheel_violations(wheel))
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Distribution file boundaries passed; import policy is checked by lint-imports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

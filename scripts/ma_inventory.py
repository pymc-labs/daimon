"""Offline AST inventory of Managed Agents SDK calls (Python >=3.12).

Run ``uv run python scripts/ma_inventory.py --output scripts/ma_inventory.json``
after moving calls into mux drivers. Commit a lower baseline with the move;
never raise the production count. Ancillary Messages API calls are excluded.

The initial snapshot reproduces 246 production calls in 79 files at integration
75d73af9f2827470fe0cb8c6f218af1da678bb2c, with anthropic==0.117.0 (uv.lock).
The ratchet is collected by the normal suite in tests/parity/test_ma_call_ratchet.py.
No SDK import, credentials, settings change, or network access is needed.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Literal, TypedDict

Category = Literal["production", "test", "test-support", "script/spike"]
RESOURCES = {"agents", "sessions", "environments", "skills", "files", "vaults", "memory_stores"}
DRIVERS = Path("packages/mux/mux/drivers")
ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts/ma_inventory.json"
_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "plugin-seed"}


class Call(TypedDict):
    file: str
    line: int
    end: int | None
    scope: str
    category: Category
    resource: str
    call: str
    request_keys: list[str]
    response_access: list[str]


class Import(TypedDict):
    file: str
    line: int
    module: str
    names: list[str]
    category: Category


class Inventory(TypedDict):
    files_scanned: int
    parse_errors: list[list[str]]
    calls: list[Call]
    imports: list[Import]


def category(path: Path) -> Category:
    """Keep the reference scan's categories, using repo-relative paths."""
    if (
        "tests" in path.parts
        or "tests_v2" in path.parts
        or any(part.startswith("test_") for part in path.parts)
        or path.name == "conftest.py"
    ):
        return "test"
    if path.is_relative_to("packages/testing"):
        return "test-support"
    if "scripts" in path.parts or "spikes" in path.parts:
        return "script/spike"
    return "production"


def scan(root: Path = ROOT) -> Inventory:
    """Return stable records; surface parse errors instead of silently omitting calls."""
    files: list[Path] = []
    for directory, subdirs, names in root.walk():
        subdirs[:] = sorted(name for name in subdirs if name not in _SKIP_DIRS)
        files.extend(directory / name for name in names if name.endswith(".py"))
    inventory: Inventory = {
        "files_scanned": len(files),
        "parse_errors": [],
        "calls": [],
        "imports": [],
    }
    for file in sorted(files):
        path = file.relative_to(root)
        filename = path.as_posix()
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=filename)
        except SyntaxError as error:
            inventory["parse_errors"].append([filename, str(error)])
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("anthropic"):
                inventory["imports"].append(
                    {
                        "file": filename,
                        "line": node.lineno,
                        "module": node.module or "",
                        "names": [alias.name for alias in node.names],
                        "category": category(path),
                    }
                )
            if not isinstance(node, ast.Call):
                continue
            function = ast.unparse(node.func)
            if ".beta." not in function:
                continue
            resource = function.split(".beta.", 1)[1]
            if resource.split(".", 1)[0] not in RESOURCES:
                continue
            scope: ast.AST = node
            while scope in parents and not isinstance(
                scope, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                scope = parents[scope]
            keys = {
                key.value
                for child in ast.walk(node)
                if isinstance(child, ast.Dict)
                for key in child.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            parent = parents.get(node)
            if isinstance(parent, ast.Await):
                parent = parents.get(parent)
            resultnames: list[str] = []
            if isinstance(parent, ast.Assign):
                resultnames = [ast.unparse(target) for target in parent.targets]
            elif isinstance(parent, ast.AnnAssign | ast.AsyncFor | ast.For | ast.comprehension):
                resultnames = [ast.unparse(parent.target)]
            fields = {
                ast.unparse(child)
                for child in ast.walk(scope)
                if isinstance(child, ast.Attribute)
                and any(ast.unparse(child).startswith(name + ".") for name in resultnames)
            }
            inventory["calls"].append(
                {
                    "file": filename,
                    "line": node.lineno,
                    "end": node.end_lineno,
                    "scope": scope.name
                    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef)
                    else "module",
                    "category": category(path),
                    "resource": resource,
                    "call": ast.unparse(node),
                    "request_keys": sorted(keys),
                    "response_access": sorted(fields),
                }
            )
    return inventory


def outside_driver_calls(inventory: Inventory) -> list[Call]:
    """The ratchet covers production only and permits all backend drivers."""
    return [
        call
        for call in inventory["calls"]
        if call["category"] == "production" and not Path(call["file"]).is_relative_to(DRIVERS)
    ]


def serialize(inventory: Inventory) -> str:
    return json.dumps(inventory, indent=2, ensure_ascii=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, help="Write JSON here instead of stdout")
    args = parser.parse_args()
    inventory = scan(args.root)
    if inventory["parse_errors"]:
        parser.exit(1, "Inventory incomplete: " + repr(inventory["parse_errors"]) + "\n")
    output = serialize(inventory)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

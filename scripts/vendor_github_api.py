"""Vendor the parts of GitHub's API descriptions the eval world checks answers against.

Writes, next to `daimon/core/evals/world/services/github.py`:

- `github_rest.json`: the 2xx JSON response schemas of every routed REST operation, with the
  component schemas they reach, as JSON Schema (OpenAPI `nullable` converted, prose dropped).
- `github.graphql`: GitHub's public GraphQL schema without descriptions or deprecations.

Run: uv run python scripts/vendor_github_api.py [--rest FILE] [--graphql FILE]
(local copies instead of downloading the published descriptions).
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from daimon.core.evals.world.services import github
from graphql import build_schema, print_schema
from pydantic import JsonValue

REST_URL = (
    "https://raw.githubusercontent.com/github/rest-api-description/main/"
    "descriptions/api.github.com/api.github.com.json"
)
GRAPHQL_URL = "https://docs.github.com/public/fpt/schema.docs.graphql"
OUT = Path(github.__file__).parent
_PROSE = {"description", "example", "examples", "externalDocs", "title", "summary"}
_SCHEMA_MAPS = {"properties", "patternProperties"}


def _schema(node: JsonValue) -> JsonValue:
    """One OpenAPI 3.0 schema as JSON Schema: prose dropped, nullable and refs converted."""
    if isinstance(node, list):
        return [_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, JsonValue] = {}
    for key, value in node.items():
        if key in _PROSE or key == "nullable":
            continue
        if key in _SCHEMA_MAPS:
            assert isinstance(value, dict)
            out[key] = {name: _schema(v) for name, v in value.items()}
        elif key == "$ref" and isinstance(value, str):
            out[key] = "#/$defs/" + value.rsplit("/", 1)[1]
        else:
            out[key] = _schema(value)
    if not node.get("nullable"):
        return out
    kind, enum = out.get("type"), out.get("enum")
    if isinstance(kind, str):
        out["type"] = [kind, "null"]
        if isinstance(enum, list):
            out["enum"] = [*enum, None]
        return out
    return {"anyOf": [out, {"type": "null"}]}


def _refs(node: JsonValue) -> set[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        found = {ref.rsplit("/", 1)[1]} if isinstance(ref, str) else set[str]()
        return found.union(*(_refs(v) for v in node.values()))
    if isinstance(node, list):
        return set[str]().union(*(_refs(v) for v in node))
    return set()


def trim_rest(spec: dict[str, Any]) -> dict[str, Any]:
    components = spec["components"]

    def deref(node: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in node and not node["$ref"].startswith("#/components/schemas/"):
            _, _, kind, name = node["$ref"].split("/")
            node = components[kind][name]
        return node

    operations: dict[str, JsonValue] = {}
    for route in github.ROUTES:
        operation = spec["paths"][route.template][route.method.lower()]
        schemas: dict[str, JsonValue] = {}
        for status, response in operation["responses"].items():
            content = deref(response).get("content", {}).get("application/json", {})
            if status.startswith("2") and "schema" in content:
                schemas[status] = _schema(content["schema"])
        operations[route.operation] = schemas
    defs: dict[str, JsonValue] = {}
    todo = sorted(_refs(operations))
    while todo:
        name = todo.pop()
        if name not in defs:
            defs[name] = _schema(components["schemas"][name])
            todo.extend(_refs(defs[name]))
    return {"operations": operations, "$defs": defs}


def trim_graphql(text: str) -> str:
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    text = re.sub(r'(?m)^\s*"[^"\n]*"\s*$\n', "", text)
    # GitHub deprecates fields its interfaces do not, which graphql-core refuses as invalid.
    text = re.sub(r'\s*@deprecated(\(\s*reason:\s*"(?:[^"\\]|\\.)*"\s*\))?', "", text)
    return print_schema(build_schema(text)) + "\n"


def _read(path: Path | None, url: str) -> str:
    if path is not None:
        return path.read_text()
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read().decode()


def main() -> None:
    parser = argparse.ArgumentParser(description="Vendor GitHub API descriptions.")
    parser.add_argument("--rest", type=Path)
    parser.add_argument("--graphql", type=Path)
    args = parser.parse_args()
    rest = trim_rest(json.loads(_read(args.rest, REST_URL)))
    (OUT / "github_rest.json").write_text(json.dumps(rest, sort_keys=True, indent=1) + "\n")
    (OUT / "github.graphql").write_text(trim_graphql(_read(args.graphql, GRAPHQL_URL)))


if __name__ == "__main__":
    main()

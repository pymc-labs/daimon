"""The Teams package builder turns the one manifest template into an uploadable zip."""

from __future__ import annotations

import importlib.util
import json
import stat
import struct
import zipfile
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from jsonschema import Draft4Validator

REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMA = Path(__file__).parent / "data/teams-manifest-v1.30.schema.json"
CLIENT_ID = "00000000-0000-4000-8000-000000000001"


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_package", REPO_ROOT / "scripts/teams/build_package.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _builder()


def _png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00\x00\x00\x00" * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _errors(manifest: dict[str, Any]) -> list[str]:
    validator = Draft4Validator(
        json.loads(SCHEMA.read_text()), format_checker=Draft4Validator.FORMAT_CHECKER
    )
    return [
        f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
        for e in validator.iter_errors(manifest)
    ]


def test_default_package_is_the_template_with_identity_filled_in() -> None:
    manifest = builder.render_manifest(client_id=CLIENT_ID, host="teams.example.com")
    assert _errors(manifest) == []
    assert manifest["id"] == manifest["bots"][0]["botId"] == CLIENT_ID
    assert manifest["webApplicationInfo"]["id"] == CLIENT_ID
    assert manifest["validDomains"] == ["teams.example.com"]
    template = yaml.safe_load(
        (REPO_ROOT / "docs/teams-app-manifest.yaml")
        .read_text()
        .replace("${DAIMON_TEAMS__CLIENT_ID}", CLIENT_ID)
        .replace("DAIMON_HOST", "teams.example.com")
    )
    assert {k: v for k, v in manifest.items() if k != "$schema"} == template, (
        "commands, scopes and permissions come from the template unchanged"
    )


def test_operator_names_and_policy_urls_replace_the_defaults() -> None:
    manifest = builder.render_manifest(
        client_id=CLIENT_ID.upper(),
        host="Bot.Example.org",
        name="Acme analyst",
        description="Analysis assistant for the Acme workspace.",
        developer="Acme",
        website_url="https://acme.example.org",
        privacy_url="https://acme.example.org/privacy",
        terms_url="https://acme.example.org/terms",
        version="2.0.1",
    )
    assert _errors(manifest) == []
    assert manifest["id"] == CLIENT_ID, "GUIDs are normalised"
    assert manifest["validDomains"] == ["bot.example.org"]
    assert manifest["name"] == {"short": "Acme analyst", "full": "Acme analyst"}
    assert manifest["description"]["full"] == "Analysis assistant for the Acme workspace."
    assert manifest["developer"] == {
        "name": "Acme",
        "websiteUrl": "https://acme.example.org",
        "privacyUrl": "https://acme.example.org/privacy",
        "termsOfUseUrl": "https://acme.example.org/terms",
    }
    assert manifest["version"] == "2.0.1"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"client_id": "not-a-guid"}, "--client-id"),
        ({"host": "https://teams.example.com"}, "--host"),
        ({"host": "teams.example.com/api"}, "--host"),
        ({"host": "localhost"}, "--host"),
        ({"privacy_url": "http://example.com/privacy"}, "--privacy-url"),
        ({"terms_url": "https://user:pw@example.com/terms"}, "--terms-url"),
        ({"name": "x" * 31}, "name short"),
        ({"developer": " "}, "developer name"),
        ({"version": "2"}, "--version"),
    ],
)
def test_refuses_input_teams_would_reject(overrides: dict[str, str], message: str) -> None:
    arguments = {"client_id": CLIENT_ID, "host": "teams.example.com", **overrides}
    with pytest.raises(builder.PackageError, match=message):
        builder.render_manifest(**arguments)


def test_zip_holds_the_three_files_at_its_top_level(tmp_path: Path) -> None:
    color, outline = tmp_path / "color.png", tmp_path / "outline.png"
    color.write_bytes(_png(192))
    outline.write_bytes(_png(32))
    output = tmp_path / "out/daimon-teams.zip"
    arguments = ["--client-id", CLIENT_ID, "--host", "teams.example.com"]
    arguments += ["--color", str(color), "--outline", str(outline), "--output", str(output)]
    builder.main(arguments)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with zipfile.ZipFile(output) as package:
        assert sorted(package.namelist()) == ["color.png", "manifest.json", "outline.png"]
        assert _errors(json.loads(package.read("manifest.json"))) == []
        assert package.read("outline.png") == outline.read_bytes()
    with pytest.raises(SystemExit, match="exists"):
        builder.main(arguments)


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("color.png", _png(32), "192x192"),
        ("outline.png", _png(192), "32x32"),
        ("outline.png", b"GIF89a" + b"\x00" * 40, "PNG"),
    ],
)
def test_refuses_icons_of_the_wrong_kind_or_size(
    tmp_path: Path, filename: str, data: bytes, message: str
) -> None:
    icons = {"color.png": _png(192), "outline.png": _png(32), filename: data}
    manifest = builder.render_manifest(client_id=CLIENT_ID, host="teams.example.com")
    output = tmp_path / "daimon-teams.zip"
    with pytest.raises(builder.PackageError, match=message):
        builder.write_package(manifest, icons, output)
    assert not output.exists(), "no partial zip is left behind"

#!/usr/bin/env python3
"""Build the Teams app package (a zip) from docs/teams-app-manifest.yaml.

The template is the one manifest: this fills in the bot's client ID and the
Teams host, applies optional names, descriptions and policy URLs, checks the
icons, and writes manifest.json, color.png and outline.png at the zip's top
level. The zip is created new with mode 0600; an existing file is refused.

Run from a checkout with PyYAML available, e.g. `uv run python scripts/teams/build_package.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

TEMPLATE = Path(__file__).resolve().parents[2] / "docs/teams-app-manifest.yaml"
SCHEMA_URL = "https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json"
CLIENT_ID_PLACEHOLDER = "${DAIMON_TEAMS__CLIENT_ID}"
HOST_PLACEHOLDER = "DAIMON_HOST"
ICONS = {"color.png": 192, "outline.png": 32}
_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
#: Teams manifest v1.30 length limits for the fields an operator replaces.
_MAX_LENGTH = {
    ("name", "short"): 30,
    ("name", "full"): 100,
    ("description", "short"): 80,
    ("description", "full"): 4000,
    ("developer", "name"): 32,
}


class PackageError(ValueError):
    """An input the package cannot be built from."""


def _https_url(value: str, flag: str) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise PackageError(f"{flag} must be a public https:// URL")
    return value


def render_manifest(
    *,
    client_id: str,
    host: str,
    name: str | None = None,
    full_name: str | None = None,
    description: str | None = None,
    full_description: str | None = None,
    developer: str | None = None,
    website_url: str | None = None,
    privacy_url: str | None = None,
    terms_url: str | None = None,
    version: str | None = None,
    template: Path = TEMPLATE,
) -> dict[str, Any]:
    """The template with identity filled in and the given fields replaced."""
    try:
        client_id = str(uuid.UUID(client_id))
    except ValueError:
        raise PackageError("--client-id must be the bot application's GUID") from None
    host = host.lower()
    if not _HOST.fullmatch(host):
        raise PackageError("--host must be a hostname, without https:// or a path")
    if version is not None and not _SEMVER.fullmatch(version):
        raise PackageError("--version must look like 1.2.3")
    text = template.read_text()
    if CLIENT_ID_PLACEHOLDER not in text or HOST_PLACEHOLDER not in text:
        raise PackageError("The template has lost its placeholders")
    text = text.replace(CLIENT_ID_PLACEHOLDER, client_id).replace(HOST_PLACEHOLDER, host)
    manifest: dict[str, Any] = {"$schema": SCHEMA_URL, **yaml.safe_load(text)}
    if version is not None:
        manifest["version"] = version
    for section, key, value in (
        ("name", "short", name),
        ("name", "full", full_name or name),
        ("description", "short", description),
        ("description", "full", full_description or description),
        ("developer", "name", developer),
    ):
        if value is not None:
            if not value.strip() or len(value) > _MAX_LENGTH[section, key]:
                limit = _MAX_LENGTH[section, key]
                raise PackageError(f"{section} {key} must be 1 to {limit} characters")
            manifest[section][key] = value
    for key, value, flag in (
        ("websiteUrl", website_url, "--website-url"),
        ("privacyUrl", privacy_url, "--privacy-url"),
        ("termsOfUseUrl", terms_url, "--terms-url"),
    ):
        if value is not None:
            manifest["developer"][key] = _https_url(value, flag)
    return manifest


def check_icon(filename: str, data: bytes) -> bytes:
    size = ICONS[filename]
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24 or data[12:16] != b"IHDR":
        raise PackageError(f"{filename} must be a PNG file")
    if struct.unpack(">II", data[16:24]) != (size, size):
        raise PackageError(f"{filename} must be {size}x{size} pixels")
    return data


def write_package(manifest: dict[str, Any], icons: dict[str, bytes], output: Path) -> None:
    checked = {filename: check_icon(filename, icons[filename]) for filename in ICONS}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise PackageError(f"{output} exists; choose a new file name") from None
    with os.fdopen(fd, "wb") as stream, zipfile.ZipFile(stream, "w") as package:
        package.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for filename, data in checked.items():
            package.writestr(filename, data)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--client-id", required=True, help="Bot application (client) ID")
    parser.add_argument("--host", required=True, help="Teams hostname, e.g. teams.example.com")
    parser.add_argument("--color", type=Path, required=True, help="192x192 full-colour PNG")
    parser.add_argument("--outline", type=Path, required=True, help="32x32 white-on-clear PNG")
    parser.add_argument("--output", type=Path, required=True, help="The zip to create")
    parser.add_argument("--name", help="Short name people see (30 characters at most)")
    parser.add_argument("--full-name", help="Full name; defaults to --name")
    parser.add_argument("--description", help="Short description (80 characters at most)")
    parser.add_argument("--full-description", help="Full description; defaults to --description")
    parser.add_argument("--developer", help="Developer or company name")
    parser.add_argument("--website-url")
    parser.add_argument("--privacy-url")
    parser.add_argument("--terms-url")
    parser.add_argument("--version", help="App version; raise it on every upload")
    args = parser.parse_args(argv)
    try:
        manifest = render_manifest(
            client_id=args.client_id,
            host=args.host,
            name=args.name,
            full_name=args.full_name,
            description=args.description,
            full_description=args.full_description,
            developer=args.developer,
            website_url=args.website_url,
            privacy_url=args.privacy_url,
            terms_url=args.terms_url,
            version=args.version,
        )
        icons = {"color.png": args.color.read_bytes(), "outline.png": args.outline.read_bytes()}
        write_package(manifest, icons, args.output)
    except (PackageError, OSError) as error:
        sys.exit(f"Teams package not built: {error}")
    print(f"Built {args.output} (version {manifest['version']}). Upload it in Teams.")


if __name__ == "__main__":
    main()

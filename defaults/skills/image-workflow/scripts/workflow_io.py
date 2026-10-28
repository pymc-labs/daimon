"""Sandbox workflow I/O. Canonical copy; sync with scripts/sync_workflow_runtime.py."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

OUTPUT_DIR = Path("/mnt/session/outputs")
KEY_FILE = Path("/mnt/session/uploads/.env")
MAX_BYTES = 40 * 1024 * 1024


class Failure(Exception):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def require(condition, message):
    if not condition:
        raise Failure(message)


def fields(value, allowed, required=()):
    require(isinstance(value, dict), "Expected a JSON object")
    require(not (set(value) - set(allowed)), "Unsupported field or operation")
    require(set(required) <= set(value), "Missing required field")


def identifier(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_@.:-]+", value), "Invalid resource ID"
    )
    require(value not in (".", ".."), "Invalid resource ID")
    return value


def positive(value):
    require(type(value) is int and value > 0, "Expected a positive integer ID")
    return value


def text(value):
    require(isinstance(value, str) and bool(value.strip()), "Expected nonempty text")
    return value


def key(name):
    # Mounted files are serialized as literal, shell-quoted assignments. Never source them.
    try:
        for line in KEY_FILE.read_text().splitlines():
            lhs, sep, rhs = line.partition("=")
            if sep and lhs.strip() == name:
                parts = shlex.split(rhs, comments=False)
                require(len(parts) == 1 and parts[0], f"Malformed {name}")
                require("\n" not in parts[0] and "\r" not in parts[0], f"Malformed {name}")
                return parts[0]
    except (OSError, ValueError):
        raise Failure(
            f"Cannot load {name}; use request_agent_key on this dedicated agent"
        ) from None
    raise Failure(f"Missing {name}; use request_agent_key on this dedicated agent")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(method, url, headers, body=None):
    """No retries, redirects or raw vendor error bodies (which may echo secrets)."""
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(NoRedirect).open(request, timeout=120) as response:
            data = response.read(MAX_BYTES + 1)
            require(len(data) <= MAX_BYTES, "Response exceeds size limit")
            return data
    except HTTPError as exc:
        if exc.code == 403 and urlsplit(url).hostname == "api.track.toggl.com":
            raise Failure(
                "Toggl 403: shared token owner needs workspace administrator role "
                "and access to the entry/project; check plan and locked-entry rules"
            ) from None
        raise Failure(
            f"Provider HTTP {exc.code}; check identity, scopes and resource access; "
            "reconcile target before retrying a write"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise Failure(
            "Transport failed; write outcome may be unknown. Reconcile target before retrying"
        ) from None


class Api:
    def __init__(self, base, authorization):
        self.base = base
        self.headers = {"Authorization": authorization, "Content-Type": "application/json"}

    def request(self, method, path, body=None):
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path")
        data = None if body is None else json.dumps(body).encode()
        raw = fetch(method, self.base + path, self.headers, data)
        try:
            return json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise Failure(
                "Invalid provider response; reconcile target before retrying a write"
            ) from None


def image_bytes(data):
    # Pillow is present in the default data image; fail clearly if a custom image lacks it.
    from PIL import Image

    require(isinstance(data, bytes) and 0 < len(data) <= MAX_BYTES, "Invalid image size")
    try:
        with Image.open(io.BytesIO(data)) as im:
            require(im.width * im.height <= 25_000_000, "Image exceeds 25 megapixels")
            require(im.format in ("PNG", "JPEG", "WEBP"), "Unsupported image format")
            im.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise Failure("Provider or reference image is invalid") from None
    return data


def save_png(data):
    image_bytes(data)
    require(data.startswith(b"\x89PNG\r\n\x1a\n"), "Expected PNG output")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    require(OUTPUT_DIR.resolve() == OUTPUT_DIR.absolute(), "Output directory must not be a symlink")
    # An unpredictable exclusive name plus directory-relative opens prevents path escape/overwrite.
    directory = os.open(OUTPUT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = f"workflow-{uuid.uuid4().hex}.png"
    try:
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except BaseException:
            os.unlink(name, dir_fd=directory)
            raise
    finally:
        os.close(directory)
    return str(OUTPUT_DIR / name)


def main(run, credential=None):
    parser = argparse.ArgumentParser(description="Run one explicit user-requested workflow")
    parser.add_argument("request", type=Path, help="Non-secret JSON request file")
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text())
        token = None
        if credential == "google":
            token = sys.stdin.read(16385).strip()
            require(
                token and len(token) <= 16384 and not any(c.isspace() for c in token),
                'Supply a short-lived get_cli_token("gcloud") token on stdin',
            )
        result = run(request, token) if credential else run(request)
        print(json.dumps({"ok": True, **result}))
    except Failure as exc:
        print(json.dumps({"ok": False, "error": str(exc), **exc.details}))
        return 1
    except Exception:
        # A vendor/codec exception may contain request or credential text. Never print it.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Invalid request, dependency or provider response; "
                    "check schema and reconcile any preceding write before retrying",
                }
            )
        )
        return 1
    return 0

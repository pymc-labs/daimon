#!/usr/bin/env python3
"""Add one expiring client secret to the Teams bot's Entra application.

Plans by default and makes no Azure call. With --create it checks the Azure CLI
is signed in to the expected tenant, calls Microsoft Graph addPassword through
`az rest`, and saves the one-time secret in a new owner-only directory. Existing
credentials are never removed. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

DISPLAY_NAME_PREFIX = "daimon bot credential"


class Refusal(ValueError):
    """A fixed message that contains no secret or identifying input."""


def _az(args: list[str]) -> str:
    result = subprocess.run(["az", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise Refusal("Azure CLI call failed; check the saved request before retrying")
    return result.stdout


def _new_state_dir(path: Path) -> Path:
    state = path.resolve()
    if state.exists():
        raise Refusal("The state directory must not exist yet")
    if any((parent / ".git").exists() for parent in (state, *state.parents)):
        raise Refusal("Keep the state directory outside every git checkout")
    return state


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def create(client_id: str, tenant_id: str, valid_days: int, state_dir: Path, apply: bool) -> str:
    client_id = str(uuid.UUID(client_id))
    tenant_id = str(uuid.UUID(tenant_id))
    if not 1 <= valid_days <= 365:
        raise Refusal("--valid-days must be between 1 and 365")
    state = _new_state_dir(state_dir)
    if not apply:
        return "Plan: add one expiring secret to the bot application. No Azure call made."
    if _az(["account", "show", "--query", "tenantId", "-o", "tsv"]).strip() != tenant_id:
        raise Refusal("The Azure CLI is signed in to another tenant; nothing created")
    state.mkdir(mode=0o700, parents=True)
    expires = datetime.now(UTC) + timedelta(days=valid_days)
    request = {
        "passwordCredential": {
            "displayName": f"{DISPLAY_NAME_PREFIX} {uuid.uuid4().hex[:12]}",
            "endDateTime": expires.isoformat().replace("+00:00", "Z"),
        }
    }
    # Saved first: after an unclear network failure, find the key by this name.
    _write_private(state / "request.json", json.dumps(request, indent=2) + "\n")
    response = json.loads(
        _az(
            [
                "rest",
                "--method",
                "POST",
                "--uri",
                f"https://graph.microsoft.com/v1.0/applications(appId='{client_id}')/addPassword",
                "--headers",
                "Content-Type=application/json",
                "--body",
                json.dumps(request),
                "-o",
                "json",
            ]
        )
    )
    secret = response.get("secretText")
    if not isinstance(secret, str) or not secret or any(c in secret for c in "\r\n\x00"):
        raise Refusal("Graph returned no usable secret; revoke the new key by its name")
    # No trailing newline, so `az keyvault secret set --file` stores it as is.
    _write_private(state / "client-secret.txt", secret)
    metadata = {key: response.get(key) for key in ("keyId", "displayName", "endDateTime")}
    metadata |= {"clientId": client_id, "tenantId": tenant_id}
    _write_private(state / "credential-metadata.json", json.dumps(metadata, indent=2) + "\n")
    return "New bot secret saved in the state directory. Existing secrets were kept."


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True, help="Bot application (client) ID")
    parser.add_argument("--tenant-id", required=True, help="Directory (tenant) ID")
    parser.add_argument("--valid-days", type=int, default=90)
    parser.add_argument("--state", type=Path, required=True, help="New directory outside git")
    parser.add_argument("--create", action="store_true", help="Call Graph and add the secret")
    args = parser.parse_args()
    try:
        print(create(args.client_id, args.tenant_id, args.valid_days, args.state, args.create))
    except Refusal as error:
        raise SystemExit(f"Refused: {error}") from None
    except Exception:
        # Subprocess and parsing errors can carry identifiers or secret text.
        raise SystemExit(
            "Refused or incomplete. Check the state directory before retrying; nothing printed."
        ) from None


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate the Azure stack's own secrets for a fresh install, and set them in Key Vault.

Writes `mcp-jwt-secret` and `crypto-keys` (one Fernet key) to a new owner-only
directory, and copies in `teams-client-secret` when given the bot secret file.
Makes no Azure call unless --vault is given. With --vault it refuses if any of
these secrets already exists there, then sets each from its file. It never
overwrites a secret: existing installs keep their keys.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import stat
import subprocess
from pathlib import Path


class Refusal(ValueError):
    """A fixed message that contains no secret input."""


def _read_private(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refusal("Secret input files must be your own regular files, mode 0600")
        value = stream.read().rstrip("\r\n")
    if not value or any(c in value for c in "\r\n\x00"):
        raise Refusal("A secret input file must hold one value on one line")
    return value


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def _az(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["az", *args], capture_output=True, text=True, check=False)


def prepare(state_dir: Path, teams_secret_file: Path | None) -> dict[str, Path]:
    state = state_dir.resolve()
    if state.exists():
        raise Refusal("The state directory must not exist yet; it never regenerates keys")
    if any((parent / ".git").exists() for parent in (state, *state.parents)):
        raise Refusal("Keep the state directory outside every git checkout")
    values = {
        "mcp-jwt-secret": secrets.token_hex(32),
        "crypto-keys": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }
    if teams_secret_file is not None:
        values["teams-client-secret"] = _read_private(teams_secret_file)
    state.mkdir(mode=0o700, parents=True)
    files: dict[str, Path] = {}
    for name, value in values.items():
        # No trailing newline: `az keyvault secret set --file` stores the file as is.
        _write_private(state / name, value)
        files[name] = state / name
    return files


def apply(vault: str, files: dict[str, Path]) -> None:
    for name in files:
        shown = _az(["keyvault", "secret", "show", "--vault-name", vault, "--name", name])
        if shown.returncode == 0:
            raise Refusal(f"{name} already exists in the vault; nothing was set")
        if "SecretNotFound" not in shown.stderr:
            raise Refusal("Could not read the vault; check your sign-in and Key Vault role")
    for name, path in files.items():
        result = _az(
            ["keyvault", "secret", "set", "--vault-name", vault, "--name", name]
            + ["--file", str(path), "--output", "none"]
        )
        if result.returncode:
            raise Refusal(f"Setting {name} failed; earlier secrets may be set, compare the vault")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="New directory outside git")
    parser.add_argument(
        "--teams-client-secret-file", type=Path, help="The bot secret, e.g. client-secret.txt"
    )
    parser.add_argument("--vault", help="Key Vault name (keyVaultName output) to set them in")
    args = parser.parse_args()
    try:
        files = prepare(args.state, args.teams_client_secret_file)
        if args.vault:
            apply(args.vault, files)
            print(f"Set {', '.join(files)} in the vault. Back up the state directory.")
        else:
            print(f"Prepared {', '.join(files)}. No Azure call made.")
    except Refusal as error:
        raise SystemExit(f"Refused: {error}") from None
    except Exception:
        message = "Refused or incomplete. Check the state directory; nothing printed."
        raise SystemExit(message) from None


if __name__ == "__main__":
    main()

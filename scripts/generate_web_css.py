"""Compile MCP browser CSS with the pinned Linux x64 Node-free Tailwind CLI.

Run: uv run python scripts/generate_web_css.py [--check]
Set DAIMON_WEB_TAILWIND_CLI to an existing copy of the pinned binary.
Use Linux x64 (locally or in CI); the downloaded binary is verified by SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "packages/adapters/mcp/daimon/adapters/mcp/static"
SOURCE = STATIC / "input.css"
OUTPUT = STATIC / "web.css"
VERSION = "v4.3.3"
SHA256 = "dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a"
URL = (
    f"https://github.com/tailwindlabs/tailwindcss/releases/download/{VERSION}/tailwindcss-linux-x64"
)


def _verified_binary() -> Path:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise SystemExit("The pinned Tailwind binary supports Linux x64; use CI on this platform.")
    override = os.environ.get("DAIMON_WEB_TAILWIND_CLI")
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    binary = (
        Path(override) if override else cache / "daimon-web" / f"tailwindcss-{VERSION}-linux-x64"
    )
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not binary.exists():
        with (
            urllib.request.urlopen(URL, timeout=120) as response,
            tempfile.NamedTemporaryFile(dir=binary.parent, delete=False) as downloaded,
        ):
            temp_path = Path(downloaded.name)
            try:
                while chunk := response.read(1024 * 1024):
                    downloaded.write(chunk)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        if hashlib.sha256(temp_path.read_bytes()).hexdigest() != SHA256:
            temp_path.unlink(missing_ok=True)
            raise SystemExit("Tailwind download failed SHA-256 verification")
        temp_path.chmod(0o755)
        temp_path.replace(binary)
    if hashlib.sha256(binary.read_bytes()).hexdigest() != SHA256:
        raise SystemExit("Tailwind binary failed SHA-256 verification")
    binary.chmod(binary.stat().st_mode | 0o111)
    return binary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed CSS is stale")
    args = parser.parse_args()
    source = SOURCE.read_text()
    if '@import "tailwindcss" source(none);' not in source or '@source "../*.py"' in source:
        raise SystemExit("CSS source must scan only explicit production page modules")
    binary = _verified_binary()
    with tempfile.TemporaryDirectory() as temp:
        compiled = Path(temp) / "web.css"
        subprocess.run(
            [str(binary), "-i", str(SOURCE), "-o", str(compiled), "--minify"],
            cwd=ROOT,
            check=True,
        )
        css = compiled.read_bytes().rstrip(b"\n") + b"\n"
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_bytes() != css:
            raise SystemExit("web.css is stale; run scripts/generate_web_css.py")
        print("web.css is current")
    else:
        OUTPUT.write_bytes(css)
        print(f"Wrote {OUTPUT.relative_to(ROOT)} ({len(css)} bytes)")


if __name__ == "__main__":
    main()

"""Prepare the dedicated staging user's private Chromium profile.

Login is manual. This module never reads or exports cookies or user tokens.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

STAGING_GUILD = "1435062989119295640"
DEFAULT_ROOT = Path.home() / ".local/state/daimon-qa-journeys"


def prepare(root: Path = DEFAULT_ROOT) -> Path:
    root = root.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ValueError("Keep the login profile outside every Git checkout")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    profile = root / "discord-staging-profile"
    profile.mkdir(mode=0o700, exist_ok=True)
    profile.chmod(0o700)
    manifest = root / "profile.json"
    expected = {
        "environment": "staging",
        "guild_id": STAGING_GUILD,
        "profile": str(profile),
        "purpose": "one dedicated staging QA user; manual login only",
        "viewport": {"width": 1440, "height": 1000},
        "locale": "en-US",
        "timezone_id": "UTC",
        "color_scheme": "dark",
    }
    if manifest.exists() and json.loads(manifest.read_text()) != expected:
        raise ValueError("Existing profile manifest differs; inspect it before reuse")
    manifest.write_text(json.dumps(expected, indent=2) + "\n")
    manifest.chmod(0o600)
    return profile


def login(root: Path = DEFAULT_ROOT) -> None:
    # Optional dependency: ordinary offline tests need no browser installation.
    from playwright.sync_api import sync_playwright

    profile = prepare(root)
    os.umask(0o077)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            timezone_id="UTC",
            color_scheme="dark",
            accept_downloads=False,
            args=["--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://discord.com/login")
        print("Dedicated staging QA login is open. Close the browser when finished.", flush=True)
        # A manual login session has no scripted input, traces, or screenshots.
        try:
            page.wait_for_event("close", timeout=0)
        finally:
            context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "login"])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.root))
    else:
        login(args.root)


if __name__ == "__main__":
    main()

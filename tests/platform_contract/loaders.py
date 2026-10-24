"""Load the helpers the adapters' own tests use to build realistic inputs.

Those test modules sit outside any importable package, so they are loaded by path
under a private name. Loading is cached: each module is executed once per process.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SLACK_TESTS = REPO_ROOT / "packages" / "adapters" / "slack" / "tests"
DISCORD_TESTS = REPO_ROOT / "packages" / "adapters" / "discord" / "tests"


def load_test_module(path: Path) -> ModuleType:
    name = "platform_contract_loaded_" + "_".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion on a private event loop in a worker thread.

    Builders are plain functions, but some payloads only come out of an async
    lifecycle. A separate thread keeps this safe whether or not the caller's
    thread already runs a loop, and leaves that thread's loop state untouched.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def call_in_event_loop[T](function: Callable[[], T]) -> T:
    """Call `function` while an event loop is running (discord.py views need one)."""

    async def call() -> T:
        return function()

    return run_async(call())

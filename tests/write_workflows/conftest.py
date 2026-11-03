from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "defaults" / "skills"


@pytest.fixture
def load_workflow(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.syspath_prepend(str(SCRIPTS / "workspace-write" / "scripts"))

    def load(skill, name):
        path = SCRIPTS / skill / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    return load


class FakeApi:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return next(self.responses)


@pytest.fixture
def fake_api():
    return FakeApi

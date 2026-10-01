"""Backend tables cover every known backend, and only those."""

from __future__ import annotations

from mux.backends import BACKENDS
from mux.backends.anthropic import BACKEND_ID as ANTHROPIC_ID
from mux.backends.google import BACKEND_ID as GOOGLE_ID
from mux.backends.openai import BACKEND_ID as OPENAI_ID
from mux.capabilities import BACKEND_IDS


def test_table_covers_all_backend_ids() -> None:
    assert set(BACKENDS) == set(BACKEND_IDS)


def test_backend_id_constants_match_table_keys() -> None:
    assert ANTHROPIC_ID == "anthropic"
    assert OPENAI_ID == "openai"
    assert GOOGLE_ID == "google"
    assert BACKENDS[ANTHROPIC_ID].can_steer
    assert not BACKENDS[GOOGLE_ID].durable_fs

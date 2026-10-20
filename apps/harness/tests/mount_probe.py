"""Refuse a real, generically named external credential mount before UID drop."""

import os
from pathlib import Path

from daimon_harness.service import create_app

assert os.geteuid() == 0
mounted = Path("/mnt/model.env")
assert mounted.is_file() and mounted.stat().st_mode & 0o777 == 0o644
os.environ["HARNESS_TOKEN"] = "synthetic-mount-probe-token"
os.environ["AZURE_OPENAI_API_KEY"] = "synthetic-mount-probe-key"
try:
    create_app(home=Path("/home/harness"))
except RuntimeError as exc:
    assert "not be readable" in str(exc), str(exc)
else:
    raise AssertionError("accepted a tool-readable /mnt/model.env mount")
assert os.geteuid() == 0, "mount refusal must precede tool startup and UID drop"

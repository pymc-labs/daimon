"""Explicit pytest entry point; normal test runs exclude this marker."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from .harness import Transcript, replay


@pytest.mark.judge
def test_recorded_transcripts() -> None:
    source = os.environ.get("DAIMON_JUDGE_RECORDINGS")
    output = os.environ.get("DAIMON_JUDGE_OUTPUT")
    if not source or not output:
        pytest.skip("set DAIMON_JUDGE_RECORDINGS and DAIMON_JUDGE_OUTPUT for a manual run")
    recordings = [Transcript.model_validate(r) for r in json.loads(Path(source).read_text())]
    result = replay(recordings, reps=int(os.environ.get("DAIMON_JUDGE_REPS", "3")))
    Path(output).write_text(json.dumps(result, indent=2) + "\n")
    assert result["blocked"] is False

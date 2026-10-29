"""The rule that stops a watch whose source keeps failing."""

from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.stores.watches import is_failed

NOW = datetime(2026, 10, 29, tzinfo=UTC)


@pytest.mark.parametrize(
    ("errors", "failing_for", "failed"),
    [
        (4, timedelta(days=7), False),
        (5, timedelta(hours=23, minutes=59, seconds=59), False),
        (5, timedelta(hours=24), True),
        (12, timedelta(days=2), True),
        (0, timedelta(days=2), False),
    ],
)
def test_five_errors_spanning_a_day_fail_the_watch(
    errors: int, failing_for: timedelta, failed: bool
) -> None:
    assert is_failed(errors, failing_since=NOW - failing_for, now=NOW) is failed

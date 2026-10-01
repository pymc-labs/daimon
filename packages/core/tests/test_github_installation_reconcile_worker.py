"""Worker retry bounds for durable GitHub installation refreshes."""

from datetime import timedelta

from daimon.core.github_installation_reconcile import _retry_delay


def test_retry_delay_stays_capped_after_many_failures() -> None:
    assert _retry_delay(10**6) == timedelta(minutes=5)

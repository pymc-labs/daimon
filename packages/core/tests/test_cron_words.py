"""`schedule_words` and `routine_phrase`: words only where the cron says exactly that."""

from __future__ import annotations

import pytest
from daimon.core.cron_words import routine_phrase, schedule_words


@pytest.mark.parametrize(
    ("cron", "words", "phrase"),
    [
        ("0 9 * * *", "every day at 09:00 UTC", "daily routine at 09:00 UTC"),
        ("30 7 * * *", "every day at 07:30 UTC", "daily routine at 07:30 UTC"),
        ("00 09 * * *", "every day at 09:00 UTC", "daily routine at 09:00 UTC"),
        ("0 9 * * 1-5", "weekdays at 09:00 UTC", "weekday routine at 09:00 UTC"),
        ("0 9 * * mon-fri", "weekdays at 09:00 UTC", "weekday routine at 09:00 UTC"),
        ("0 9 * * 1", "every Monday at 09:00 UTC", "Monday routine at 09:00 UTC"),
        ("0 9 * * MON", "every Monday at 09:00 UTC", "Monday routine at 09:00 UTC"),
        ("15 18 * * 0", "every Sunday at 18:15 UTC", "Sunday routine at 18:15 UTC"),
        ("15 18 * * 7", "every Sunday at 18:15 UTC", "Sunday routine at 18:15 UTC"),
        ("0 * * * *", "every hour (UTC)", "hourly routine (UTC)"),
    ],
)
def test_simple_schedules_become_words(cron: str, words: str, phrase: str) -> None:
    assert schedule_words(cron, "UTC") == words
    assert routine_phrase(cron, "UTC") == phrase


@pytest.mark.parametrize(
    "cron",
    [
        "*/15 * * * *",  # a step
        "15 * * * *",  # hourly, but not on the hour
        "0 * * * 1-5",  # hourly on weekdays only
        "0 9,17 * * *",  # a list of hours
        "0 9-17 * * *",  # a range of hours
        "0 9 * * 1,3",  # a list of days
        "0 9 * * 2-4",  # a range that is not Monday to Friday
        "0 9 1 * *",  # a day of the month
        "0 9 * 6 *",  # a month
        "0 24 * * *",  # no such hour
        "60 9 * * *",  # no such minute
        "0 9 * * 8",  # no such day
        "0 0 9 * * *",  # six fields
        "@daily",  # a nickname
        "",
    ],
)
def test_anything_else_stays_cron_text(cron: str) -> None:
    assert schedule_words(cron, "UTC") == f"{cron} (UTC)", "never words that could mislead"
    assert routine_phrase(cron, "UTC") == f"routine ({cron}, UTC)"


def test_the_timezone_is_always_kept() -> None:
    assert schedule_words("0 9 * * *", "Europe/London") == "every day at 09:00 Europe/London"
    assert schedule_words("0 * * * *", "Asia/Kolkata") == "every hour (Asia/Kolkata)"
    assert schedule_words("0 9 * * 1,3", "America/New_York") == "0 9 * * 1,3 (America/New_York)"

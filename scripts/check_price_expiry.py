"""Warn, without failing, about tariffs that lapse soon or already have.

A model past its `valid_before` is refused at admission, so a lapsing tariff needs a
fresh price check. Prints one GitHub `::warning::` per tariff within WARN_DAYS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.pricing import price_expiries

WARN_DAYS = 30


def main() -> None:
    today = datetime.now(UTC).date()
    for profile, model, valid_before in price_expiries():
        if valid_before > today + timedelta(days=WARN_DAYS):
            continue
        when = "has lapsed" if valid_before <= today else "lapses"
        print(f"::warning title=Tariff {when}::{profile} {model} {when} on {valid_before}")


if __name__ == "__main__":
    main()

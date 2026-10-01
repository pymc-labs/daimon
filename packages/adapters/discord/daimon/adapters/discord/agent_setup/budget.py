"""Discord's hard component ceilings, named once for the setup panel.

discord.py 2.7.1 refuses a ``LayoutView`` above 40 total components, counting
nested ones: a ``Container`` costs itself plus everything inside it, a
``Section`` costs 2 (itself and its required accessory) plus its children, and
an ``ActionRow`` costs itself plus up to five items. 4000 display characters
across every ``TextDisplay`` is the other wall, and a select holds 25 options —
which is why the roster pages with buttons instead of offering one unbounded
select.

The page sizes are what is left after the chrome is paid for. They are asserted
against a rendered worst-case page rather than trusted, so a renderer that grows
a line fails a test instead of failing in front of a user.
"""

from __future__ import annotations

from typing import Final

LAYOUT_COMPONENT_BUDGET: Final = 40
"""Total components a LayoutView may carry, nested ones included."""

LAYOUT_TEXT_BUDGET: Final = 4000
"""Display characters a LayoutView may carry across all of its TextDisplays."""

ROSTER_ROW_COST: Final = 3
"""One roster row: a Section (itself + its required Details accessory) + one TextDisplay."""

ROSTER_CHROME_COST: Final = 13
"""Everything in the roster view that is not a row, at its widest.

Container 1 + header TextDisplay 1 + thread-context line 1 + hairline 1 +
three ActionRows at 1 + 2 buttons each 9 = 13.
``ROSTER_PAGE_SIZE`` rows cost 24, so the widest roster renders 37 of the 40
available components.
"""

ROSTER_PAGE_SIZE: Final = 8
"""Roster rows per page — the largest page that fits inside the budget."""

ROUTING_PAGE_SIZE: Final = 20
"""Channel lines per page on Who answers where; those are text, not components."""

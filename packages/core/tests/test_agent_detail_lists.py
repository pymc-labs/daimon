"""Long detail lists keep complete entries and share a bounded display."""

import pytest
from daimon.core.agent_detail_lists import format_detail_lists


@pytest.mark.parametrize("count", [0, 1, 5, 6, 7, 61])
def test_collapsed_list_shows_six_complete_entries(count: int) -> None:
    entries = [f"`KEY_{index}`" for index in range(count)]
    text = format_detail_lists({"keys": entries}, expanded=None, max_chars=4000)["keys"]
    expected = entries[:6]
    if count > 6:
        expected.append(f"+{count - 6} more")
    assert text == "\n".join(expected), (
        "collapsed lists show complete entries and an exact remainder"
    )


def test_expansion_caps_at_sixty_with_an_accurate_remainder() -> None:
    entries = [f"Skill {index}" for index in range(75)]
    text = format_detail_lists({"skills": entries}, expanded="skills", max_chars=4000)["skills"]
    assert text.splitlines() == entries[:60] + ["+15 more"], "expansion respects the entry cap"


def test_expansion_preserves_the_other_lists_default_entries() -> None:
    items = {
        "keys": [f"KEY_{index}" for index in range(70)],
        "skills": [f"Skill {index}" for index in range(10)],
        "connections": [f"[Server {index}](https://example.com/{index})" for index in range(10)],
    }
    rendered = format_detail_lists(items, expanded="keys", max_chars=420)
    assert sum(map(len, rendered.values())) <= 420, "list text must fit its shared allowance"
    assert rendered["skills"].splitlines() == items["skills"][:6] + ["+4 more"], (
        "expanding keys does not consume the skills default rows"
    )
    assert rendered["connections"].splitlines() == items["connections"][:6] + ["+4 more"], (
        "expanding keys reserves the connections default rows"
    )
    assert rendered["keys"].endswith(" more"), "budget-limited expansions report hidden entries"


def test_oversized_link_is_omitted_whole_instead_of_cut() -> None:
    link = "[Service](https://example.com/" + "x" * 5000 + ")"
    rendered = format_detail_lists({"connections": [link]}, expanded="connections", max_chars=100)
    assert rendered["connections"] == "+1 more", "a hyperlink must never be sliced"


def test_oversized_collapsed_lists_share_space_without_hiding_other_categories() -> None:
    items = {"keys": ["K" * 70] * 8, "skills": ["S" * 70] * 8, "connections": ["C" * 70] * 8}
    rendered = format_detail_lists(items, expanded=None, max_chars=300)
    assert sum(map(len, rendered.values())) <= 300, "all categories share the total allowance"
    assert all(text.endswith("+7 more") for text in rendered.values()), (
        "each category retains a complete entry and its hidden count"
    )


def test_individual_section_limit_applies_even_when_total_space_remains() -> None:
    entries = ["Skill " + "x" * 70 for _ in range(60)]
    rendered = format_detail_lists(
        {"skills": entries}, expanded="skills", max_chars=9000, max_list_chars=300
    )
    assert len(rendered["skills"]) <= 300, "a section cannot use another section's allowance"
    assert rendered["skills"].splitlines() == entries[:3] + ["+57 more"], (
        "the limit includes the remainder line"
    )

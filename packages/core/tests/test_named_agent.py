"""The name syntax has one exact, Unicode-normalized token."""

from daimon.core.named_agent import matching_agent, name_after_mention
from daimon.testing.ma_models import ma_agent


def test_name_after_bot_mention_only_accepts_a_colon_suffix() -> None:
    assert name_after_mention("<@bot> Planner: draft", "<@bot>") == "Planner"
    assert name_after_mention("  planner: draft") == "planner"
    assert name_after_mention("<@bot> planner draft", "<@bot>") is None
    assert name_after_mention("please ask <@bot> planner: draft", "<@bot>") == "planner"


def test_agent_name_match_is_case_insensitive_nfkc_and_unambiguous() -> None:
    agent = ma_agent(id="agent1", name="Planner")
    assert matching_agent([agent], "ＰＬＡＮＮＥＲ") == agent
    assert matching_agent([agent], "plan") is None
    assert matching_agent([agent, ma_agent(id="agent2", name="planner")], "planner") is None

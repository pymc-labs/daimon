from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from daimon.adapters.discord.lifecycle import _with_ping  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.discord.reactions import DONE, WORKING, arrange_reactions


async def test_done_goes_first_and_the_model_emoji_are_added_again_after_it() -> None:
    fetched = MagicMock()
    fetched.reactions = [SimpleNamespace(emoji=e, me=True) for e in (WORKING, "🌒")]
    fetched.remove_reaction, fetched.add_reaction = AsyncMock(), AsyncMock()
    message = MagicMock(guild=SimpleNamespace(me="bot"), fetch=AsyncMock(return_value=fetched))
    await arrange_reactions(message, status=DONE)
    assert [c.args[0] for c in fetched.remove_reaction.await_args_list] == [WORKING, "🌒"]
    assert [c.args[0] for c in fetched.add_reaction.await_args_list] == [DONE, "🌒"]


def test_ping_is_joined_with_a_space_and_replaces_one_the_model_wrote() -> None:
    assert _with_ping(7, "<@7> Done.") == "<@7> Done."
    assert _with_ping(7, "<@!7>Done.") == "<@7> Done."
    assert _with_ping(7, "Done.") == "<@7> Done."
    assert _with_ping(7, "| a |\n| - |") == "<@7>\n| a |\n| - |"

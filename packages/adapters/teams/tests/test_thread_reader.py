"""Which window a channel message replays, and failure that leaves the turn running."""

from __future__ import annotations

import dataclasses

import httpx
import pytest
import structlog
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.thread_reader import ThreadReader, root_id
from daimon.core.teams_graph import GraphClient, GraphUnavailable, TeamGroups

from .conftest import CHANNEL_ID, make_inbound

GROUP = "11111111-1111-1111-1111-111111111111"
ROOT = "1700000000001"


async def _token() -> str:
    return "graph-token"


async def _no_lookup(team_id: str) -> str | None:
    raise AssertionError("the activity named the group")


def _reader(handler: httpx.MockTransport) -> ThreadReader:
    graph = GraphClient(httpx.AsyncClient(transport=handler), _token)
    return ThreadReader(graph, TeamGroups(_no_lookup), bot_app_id="bot")


def _inbound(activity_id: str) -> TeamsInbound:
    inbound = make_inbound("q", conversation=f"{CHANNEL_ID};messageid={ROOT}", kind="channel")
    return dataclasses.replace(
        inbound, channel_id=CHANNEL_ID, activity_id=activity_id, team_group_id=GROUP
    )


def _message(id: str) -> dict[str, object]:
    return {"id": id, "body": {"contentType": "text", "content": f"m{id}"}}


def test_root_id_reads_the_thread_root_or_none() -> None:
    assert root_id(f"{CHANNEL_ID};messageid={ROOT}") == ROOT
    assert root_id("a:personal-chat") is None


async def test_a_mention_starting_a_thread_replays_the_channel() -> None:
    paths: list[str] = []
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        queries.append(dict(request.url.params))
        return httpx.Response(200, json={"value": [_message(ROOT), _message("1699999999999")]})

    block = await _reader(httpx.MockTransport(handler)).read(
        _inbound(ROOT),
        watermark=None,
        skip_ids=frozenset(),
    )
    assert paths == [f"/v1.0/teams/{GROUP}/channels/{CHANNEL_ID}/messages"]
    assert queries == [{"$top": "25", "$expand": "replies"}], "each post comes with its replies"
    assert block is not None and block.tag == "channel_context"
    assert block.attrs == {"count": "1"}, "the trigger post itself is not context"


async def test_a_continuation_reads_only_the_delta_since_the_watermark() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"value": [_message("1700000000009"), _message("5")]})

    block = await _reader(httpx.MockTransport(handler)).read(
        _inbound("1700000000010"),
        watermark="1700000000005",
        skip_ids=frozenset(),
    )
    assert paths == [f"/v1.0/teams/{GROUP}/channels/{CHANNEL_ID}/messages/{ROOT}/replies"]
    assert block is not None and block.tag == "thread_delta" and len(block.lines) == 1


async def test_a_refused_read_logs_one_warning_without_content_and_marks_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "secret thread text"}})

    with structlog.testing.capture_logs() as logs:
        block = await _reader(httpx.MockTransport(handler)).read(
            _inbound("1700000000010"),
            watermark=None,
            skip_ids=frozenset(),
        )
    assert block is not None and block.unavailable == "http error", "the turn runs, told why"
    assert block.lines == ()
    assert logs == [
        {
            "event": "teams.history.unavailable",
            "log_level": "warning",
            "status": 403,
            "reason": "http error",
        }
    ]


async def test_the_classifier_window_reads_the_replies_and_the_root() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/replies"):
            return httpx.Response(200, json={"value": [_message("1700000000003")]})
        return httpx.Response(200, json=_message(ROOT))

    window = await _reader(httpx.MockTransport(handler)).read_window(
        _inbound("1700000000003"), exclude_ids=frozenset({"1700000000003"}), limit=10
    )
    base = f"/v1.0/teams/{GROUP}/channels/{CHANNEL_ID}/messages/{ROOT}"
    assert paths == [f"{base}/replies", base], "a short thread includes its root post"
    assert [m.content for m in window] == [f"m{ROOT}"], "the burst itself is not the window"


async def test_an_unreadable_classifier_window_raises_for_the_caller_to_stay_silent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(GraphUnavailable):
        await _reader(httpx.MockTransport(handler)).read_window(
            _inbound("1700000000003"), exclude_ids=frozenset(), limit=10
        )


async def test_read_media_reads_every_message_a_composed_turn_answers() -> None:
    """Queued messages fold into one turn: each one's hosted images are read, not only the last."""
    paths: list[str] = []
    replies = f"/v1.0/teams/{GROUP}/channels/{CHANNEL_ID}/messages/{ROOT}/replies"

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        message_id = request.url.path.rsplit("/", 1)[-1]
        src = f"https://graph.microsoft.com{replies}/{message_id}/hostedContents/h/$value"
        body = {"contentType": "html", "content": f'<img src="{src}">'}
        return httpx.Response(200, json={"id": message_id, "body": body})

    inbound = dataclasses.replace(_inbound("1700000000005"), composed_ids=("1700000000004",))
    media = await _reader(httpx.MockTransport(handler)).read_media(inbound)

    assert paths == [f"{replies}/1700000000004", f"{replies}/1700000000005"], "oldest first"
    assert media is not None and len(media.image_urls) == 2, "the earlier message's image too"


async def test_a_reseed_keeps_the_images_already_inlined() -> None:
    """The images went with the first message; a reseed may not claim new ones."""
    image = (
        f"https://graph.microsoft.com/v1.0/teams/{GROUP}/channels/{CHANNEL_ID}/messages/{ROOT}"
        "/hostedContents/aWQ9eA==/$value"
    )
    root = {"id": ROOT, "body": {"contentType": "html", "content": f'<img src="{image}">'}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/replies"):
            return httpx.Response(200, json={"value": [_message("1700000000005")]})
        return httpx.Response(200, json=root)

    reader = _reader(httpx.MockTransport(handler))
    first = await reader.read(_inbound("1700000000010"), watermark=None, skip_ids=frozenset())
    assert first is not None and first.image_urls == (image,)
    assert first.attached.images == {ROOT: 1}
    again = await reader.read(
        _inbound("1700000000010"), watermark=None, skip_ids=frozenset(), images={}
    )
    assert again is not None and again.image_urls == () and not again.attached.images


async def test_a_card_whose_post_lost_its_id_is_found_by_its_cancel_key() -> None:
    other = "22222222-2222-2222-2222-222222222222"
    paths: list[str] = []

    def reply(id: str, app: str, marker: str) -> dict[str, object]:
        content = f'{{"data": {{"action": "cancel_turn", "turn": "{marker}"}}}}'
        attachment = {"contentType": "application/vnd.microsoft.card.adaptive", "content": content}
        return {**_message(id), "from": {"application": {"id": app}}, "attachments": [attachment]}

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if other in request.url.path:
            return httpx.Response(404)
        replies = [
            reply("9", "someone-else", "k1"),
            reply("8", "bot", "k0"),
            reply("7", "bot", "k1"),
        ]
        return httpx.Response(200, json={"value": replies})

    found = await _reader(httpx.MockTransport(handler)).find_card(
        f"{CHANNEL_ID};messageid={ROOT}", "k1", group_ids=[other, GROUP]
    )
    assert found == "7", "only the bot's own card with that key"
    assert len(paths) == 2, "a team that does not hold the channel is skipped"
    assert (
        await _reader(httpx.MockTransport(handler)).find_card("a:chat", "k1", group_ids=[GROUP])
        is None
    ), "a 1:1 chat cannot be read back"

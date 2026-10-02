"""Graph client: one page per read, the host pin, and failures that carry no content."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from daimon.core.teams_graph import GraphClient, GraphUnavailable, TeamGroups, skiptoken_of

GROUP = "11111111-1111-1111-1111-111111111111"
CHANNEL = "19:channel-1@thread.tacv2"
REPLIES = f"/v1.0/teams/{GROUP}/channels/19%3Achannel-1%40thread.tacv2/messages/100/replies"


async def _token() -> str:
    return "graph-token"


def _client(
    handler: Callable[[httpx.Request], httpx.Response], seen: list[httpx.Request] | None = None
) -> GraphClient:
    def record(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return GraphClient(httpx.AsyncClient(transport=httpx.MockTransport(record)), _token)


def _message(id: str, text: str = "hi") -> dict[str, object]:
    return {
        "id": id,
        "messageType": "message",
        "createdDateTime": "2026-01-01T00:00:00Z",
        "from": {"user": {"id": "u-1", "displayName": "Ada"}},
        "body": {"contentType": "html", "content": f"<p>{text}</p>"},
    }


async def test_list_replies_reads_one_page_with_the_token_and_marks_more() -> None:
    seen: list[httpx.Request] = []
    body = {"value": [_message("102"), _message("101")], "@odata.nextLink": "https://x/next"}
    page = await _client(lambda _: httpx.Response(200, json=body), seen).list_replies(
        GROUP, CHANNEL, "100"
    )

    [request] = seen
    assert request.url.host == "graph.microsoft.com"
    assert request.url.raw_path.decode().split("?")[0] == REPLIES, "ids escaped per segment"
    assert request.url.params["$top"] == "50", "one page, at Graph's ceiling"
    assert request.headers["Authorization"] == "Bearer graph-token"
    assert [m.id for m in page.value] == ["102", "101"]
    assert page.next_link is not None, "more replies exist, so the caller can mark truncation"


async def test_channel_posts_expand_their_replies_and_a_next_link_stays_on_graph() -> None:
    seen: list[httpx.Request] = []
    post = {**_message("100"), "replies": [_message("101")], "replies@odata.nextLink": "x"}
    client = _client(lambda _: httpx.Response(200, json={"value": [post]}), seen)
    page = await client.list_channel_messages(GROUP, CHANNEL, top=5, expand_replies=True)
    assert seen[0].url.params["$expand"] == "replies"
    assert [r.id for r in page.value[0].replies] == ["101"]
    assert page.value[0].replies_next_link == "x", "a post with more replies says so"
    await client.next_page(
        "https://graph.microsoft.com/v1.0/teams/x/channels/y/messages?$skiptoken=1"
    )
    assert seen[1].url.params["$skiptoken"] == "1", "the link is followed as given"
    with pytest.raises(GraphUnavailable, match="not a Graph URL"):
        await client.next_page("https://attacker.example/next")
    await client.list_replies(GROUP, CHANNEL, "100", skiptoken="tok")
    assert seen[-1].url.params["$skiptoken"] == "tok"
    assert skiptoken_of("https://graph.microsoft.com/v1.0/x?$top=5&$skiptoken=abc") == "abc"
    assert skiptoken_of(None) is None


async def test_get_message_addresses_a_reply_under_its_root() -> None:
    seen: list[httpx.Request] = []
    client = _client(lambda _: httpx.Response(200, json=_message("101")), seen)
    message = await client.get_message(GROUP, CHANNEL, "101", root_id="100")
    assert seen[0].url.path.endswith("/messages/100/replies/101"), "a reply lives under its root"
    assert message.sender is not None and message.sender.user is not None
    assert message.sender.user.display_name == "Ada"


@pytest.mark.parametrize("status", [403, 404, 429, 500])
async def test_a_failed_read_raises_with_the_status_and_no_content(status: int) -> None:
    secret = "private thread text"
    client = _client(lambda _: httpx.Response(status, json={"error": {"message": secret}}))
    with pytest.raises(GraphUnavailable) as caught:
        await client.list_channel_messages(GROUP, CHANNEL)
    assert caught.value.status == status, "the status is what gets logged"
    assert secret not in str(caught.value), "a Graph error body never reaches the logs"


async def test_an_id_cannot_climb_out_of_its_path_segment() -> None:
    seen: list[httpx.Request] = []
    client = _client(lambda _: httpx.Response(200, json=_message("1")), seen)
    await client.get_message(GROUP, CHANNEL, "../../../users")
    assert seen[0].url.raw_path.decode().endswith("/messages/..%2F..%2F..%2Fusers")


async def test_a_redirect_is_never_followed() -> None:
    seen: list[httpx.Request] = []
    moved = httpx.Response(302, headers={"Location": "https://attacker.example/steal"})
    with pytest.raises(GraphUnavailable) as caught:
        await _client(lambda _: moved, seen).list_replies(GROUP, CHANNEL, "100")
    assert caught.value.status == 302
    assert [r.url.host for r in seen] == ["graph.microsoft.com"], "the token stays on Graph"


async def test_a_timeout_or_bad_body_is_unavailable_not_an_error() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(GraphUnavailable, match="ReadTimeout"):
        await _client(slow).list_replies(GROUP, CHANNEL, "100")
    with pytest.raises(GraphUnavailable, match="unexpected body"):
        await _client(lambda _: httpx.Response(200, json={"value": "nope"})).list_replies(
            GROUP, CHANNEL, "100"
        )


async def test_a_token_failure_is_unavailable_and_sends_nothing() -> None:
    seen: list[httpx.Request] = []

    async def broken() -> str:
        raise ValueError("AADSTS7000215")

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"value": []})

    client = GraphClient(httpx.AsyncClient(transport=httpx.MockTransport(record)), broken)
    with pytest.raises(GraphUnavailable, match="token: ValueError"):
        await client.list_replies(GROUP, CHANNEL, "100")
    assert seen == [], "no request goes out without a token"


async def test_team_groups_prefer_the_activity_then_look_up_once_per_team() -> None:
    calls: list[str] = []

    async def lookup(team_id: str) -> str | None:
        calls.append(team_id)
        return GROUP if team_id == "19:team@thread.tacv2" else None

    teams = TeamGroups(lookup)
    assert await teams.group_id("19:team@thread.tacv2", known="g-known") == "g-known"
    assert await teams.group_id("19:team@thread.tacv2") == GROUP
    assert await teams.group_id("19:team@thread.tacv2") == GROUP
    assert calls == ["19:team@thread.tacv2"], "one Bot Framework lookup per team"
    with pytest.raises(GraphUnavailable, match="not found"):
        await teams.group_id("19:other@thread.tacv2")
    with pytest.raises(GraphUnavailable, match="no team"):
        await teams.group_id(None)

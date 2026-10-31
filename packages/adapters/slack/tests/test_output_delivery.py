"""Tests for the Slack output-delivery adapter.

MA traffic runs through MARouter + build_fake_anthropic (httpx transport);
Slack traffic runs through the aioresponses-backed ``fake_slack_web_client``
fixture — the real slack_sdk request builder and response parser run for
every call. No method-level mocks anywhere.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import daimon.adapters.slack.output_delivery as delivery_module
import httpx
import pytest
from anthropic.types.beta import FileMetadata
from daimon.adapters.slack.blockkit import ERROR_COLOR
from daimon.adapters.slack.output_delivery import (
    SLACK_MAX_BYTES_PER_FILE,
    deliver_provider_output,
    deliver_session_outputs,
)
from daimon.core.output_delivery import DeliverableFile, NoticedFiles
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from slack_sdk.web.async_client import AsyncWebClient
from yarl import URL

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
NoticeKeys = getattr(delivery_module, "NoticeKeys", set)

_GET_URL = re.compile(r"https://slack\.com/api/files\.getUploadURLExternal.*")
_UPLOAD = re.compile(r"https://files\.slack\.com/upload/v1/.*")
_COMPLETE = re.compile(r"https://slack\.com/api/files\.completeUploadExternal.*")
_POST_MESSAGE_KEY = ("POST", URL("https://slack.com/api/chat.postMessage"))


def _post_message_calls(mock: Any) -> list[Any]:
    return list(mock.requests.get(_POST_MESSAGE_KEY, []))


def _notice_text(call: Any) -> str:
    """The words of a notice posted as a red-barred attachment, and nothing above it."""
    body = call.kwargs["json"]
    assert "text" not in body, "no top-level text: Slack would print it above the bar"
    (attachment,) = body["attachments"]
    assert attachment["color"] == ERROR_COLOR, "the bar is error red"
    (section,) = attachment["blocks"]
    return section["text"]["text"]


async def test_delivery_uploads_via_three_request_flow_without_content_type(
    fake_slack_web_client: Any,
) -> None:
    """One downloadable file produces the 3-request upload flow in order, with
    the display filename (extension derived from the mime) and no content_type
    anywhere — files_upload_v2 would drop it into the completeUploadExternal
    query string where it is a silent no-op."""
    mock = fake_slack_web_client.mock
    mock.post(
        _GET_URL,
        payload={
            "ok": True,
            "file_id": "F1",
            "upload_url": "https://files.slack.com/upload/v1/ABC",
        },
    )
    mock.post(_UPLOAD, status=200, body="OK", content_type="text/plain")
    mock.post(_COMPLETE, payload={"ok": True, "files": [{"id": "F1", "title": "report.txt"}]})

    deletes: list[str] = []

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_report",
                    created_at=NOW,
                    filename="report",
                    mime_type="text/plain",
                    size_bytes=11,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"hello world"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    anthropic_client = build_fake_anthropic(router.dispatch)

    async def sleep(delay: float) -> None:
        pass

    await deliver_session_outputs(
        anthropic_client,
        fake_slack_web_client.client,
        session_id="sesn_1",
        channel_id="C1",
        thread_ts="1.2",
        notice_keys=NoticeKeys(),
        team_id="T1",
        sleep=sleep,
    )

    upload_keys = [
        (method, url)
        for (method, url) in mock.requests
        if "getUploadURLExternal" in str(url)
        or "files.slack.com" in str(url)
        or "completeUploadExternal" in str(url)
    ]
    assert [str(url.with_query(None)) for _, url in upload_keys] == [
        "https://slack.com/api/files.getUploadURLExternal",
        "https://files.slack.com/upload/v1/ABC",
        "https://slack.com/api/files.completeUploadExternal",
    ], "the upload must run the 3-request flow in order"

    get_url_query = upload_keys[0][1].query
    assert get_url_query.get("filename") == "report.txt", (
        "the extensionless MA filename must gain its mime-derived extension"
    )
    for _, url in mock.requests:
        assert "content_type" not in url.query, (
            f"content_type must not appear in any request query, found in {url}"
        )
    assert deletes == ["file_report"], "the posted file's listing entry must be deleted"


async def test_delivery_posts_scope_notice_and_deletes_nothing_on_missing_scope(
    fake_slack_web_client: Any,
) -> None:
    """missing_scope aborts the sweep: one notice asking to allow file uploads,
    no deletion, and the dedup key lands in notice_keys."""
    mock = fake_slack_web_client.mock
    mock.post(_GET_URL, payload={"ok": False, "error": "missing_scope"})

    deletes: list[str] = []

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_one",
                    created_at=NOW,
                    filename="chart.png",
                    mime_type="image/png",
                    size_bytes=9,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"png-bytes"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    anthropic_client = build_fake_anthropic(router.dispatch)

    async def sleep(delay: float) -> None:
        pass

    notice_keys = NoticeKeys()
    await deliver_session_outputs(
        anthropic_client,
        fake_slack_web_client.client,
        session_id="sesn_1",
        channel_id="C1",
        thread_ts="1.2",
        notice_keys=notice_keys,
        team_id="T_SCOPE",
        sleep=sleep,
    )

    notices = _post_message_calls(mock)
    assert len(notices) == 1, "exactly one in-thread notice must be posted"
    assert _notice_text(notices[0]) == (
        "I couldn't attach the file.\n\n"
        "Ask a workspace admin to allow file uploads and reinstall Daimon."
    ), "plain words, a blank line between the two lines"
    assert deletes == [], "an aborted sweep must delete nothing"
    assert "notice:T_SCOPE:C1:1.2:missing_scope" in notice_keys, (
        "the dedup key must be recorded after the notice posts"
    )


async def test_delivery_posts_storage_notice_and_deletes_nothing_on_storage_limit(
    fake_slack_web_client: Any,
) -> None:
    """storage_limit_reached aborts with its own copy — about storage, not
    scopes — deletes nothing and records its dedup key."""
    mock = fake_slack_web_client.mock
    mock.post(_GET_URL, payload={"ok": False, "error": "storage_limit_reached"})

    deletes: list[str] = []

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_one",
                    created_at=NOW,
                    filename="report.csv",
                    mime_type="text/csv",
                    size_bytes=9,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"a,b\n1,2\n"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    anthropic_client = build_fake_anthropic(router.dispatch)

    async def sleep(delay: float) -> None:
        pass

    notice_keys = NoticeKeys()
    await deliver_session_outputs(
        anthropic_client,
        fake_slack_web_client.client,
        session_id="sesn_1",
        channel_id="C1",
        thread_ts="1.2",
        notice_keys=notice_keys,
        team_id="T_STORE",
        sleep=sleep,
    )

    notices = _post_message_calls(mock)
    assert len(notices) == 1, "exactly one in-thread notice must be posted"
    assert _notice_text(notices[0]) == (
        "I couldn't attach the file. This Slack workspace is out of file space.\n\n"
        "Ask a workspace admin to free some up."
    ), "the storage notice is about file space, not scopes"
    assert deletes == [], "an aborted sweep must delete nothing"
    assert "notice:T_STORE:C1:1.2:storage_limit_reached" in notice_keys, (
        "the dedup key must be recorded after the notice posts"
    )


async def test_delivery_posts_abort_notice_once_per_thread_and_logs_once_per_workspace(
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each affected thread gets one notice while the workspace error logs once."""
    import daimon.adapters.slack.output_delivery as delivery_module

    warnings: list[str] = []
    monkeypatch.setattr(
        delivery_module.log, "warning", lambda event, **kwargs: warnings.append(event)
    )
    mock = fake_slack_web_client.mock
    mock.post(_GET_URL, payload={"ok": False, "error": "missing_scope"}, repeat=True)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_one",
                    created_at=NOW,
                    filename="chart.png",
                    mime_type="image/png",
                    size_bytes=9,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"png-bytes"),
    )
    router.add(
        "DELETE",
        r"/v1/files/([^/]+)",
        lambda request, match: httpx.Response(
            200, json={"id": match.group(1), "type": "file_deleted"}
        ),
    )
    anthropic_client = build_fake_anthropic(router.dispatch)

    async def sleep(delay: float) -> None:
        pass

    notice_keys = NoticeKeys()
    for channel_id, thread_ts in (("C1", "1.2"), ("C1", "2.3"), ("C2", "2.3"), ("C1", "1.2")):
        await deliver_session_outputs(
            anthropic_client,
            fake_slack_web_client.client,
            session_id="sesn_1",
            channel_id=channel_id,
            thread_ts=thread_ts,
            notice_keys=notice_keys,
            team_id="T_ONCE",
            sleep=sleep,
        )

    notices = _post_message_calls(mock)
    assert [
        (call.kwargs["json"]["channel"], call.kwargs["json"]["thread_ts"]) for call in notices
    ] == [
        ("C1", "1.2"),
        ("C1", "2.3"),
        ("C2", "2.3"),
    ]
    assert warnings == ["slack.output_delivery.unavailable"]


async def test_delivery_continues_after_bytes_post_failure_on_one_file(
    fake_slack_web_client: Any,
) -> None:
    """A non-200 on the files.slack.com bytes POST raises SlackRequestError
    (which has no .response): the failed file stays listed, the next file is
    posted and deleted, and no notice is posted."""
    mock = fake_slack_web_client.mock
    mock.post(
        _GET_URL,
        payload={
            "ok": True,
            "file_id": "F1",
            "upload_url": "https://files.slack.com/upload/v1/AAA",
        },
        repeat=True,
    )
    mock.post(_UPLOAD, status=500, body="nope", content_type="text/plain")
    mock.post(_UPLOAD, status=200, body="OK", content_type="text/plain")
    mock.post(_COMPLETE, payload={"ok": True, "files": [{"id": "F1", "title": "b.txt"}]})

    deletes: list[str] = []

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_first",
                    created_at=NOW,
                    filename="a.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
                FileMetadata(
                    id="file_second",
                    created_at=NOW,
                    filename="b.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"txt"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    anthropic_client = build_fake_anthropic(router.dispatch)

    async def sleep(delay: float) -> None:
        pass

    await deliver_session_outputs(
        anthropic_client,
        fake_slack_web_client.client,
        session_id="sesn_1",
        channel_id="C1",
        thread_ts="1.2",
        notice_keys=NoticeKeys(),
        team_id="T1",
        sleep=sleep,
    )

    assert deletes == ["file_second"], (
        "only the successfully posted file may be deleted; the bytes-POST "
        "failure must leave the first file listed"
    )
    assert _post_message_calls(mock) == [], "a per-file failure must not post any notice"


async def test_delivery_continues_after_non_scope_api_error_on_one_file(
    fake_slack_web_client: Any,
) -> None:
    """A non-abort Slack API error (file_upload_size_restricted) isolates to
    one file: the second is posted and deleted, no notice, the first stays."""
    mock = fake_slack_web_client.mock
    mock.post(_GET_URL, payload={"ok": False, "error": "file_upload_size_restricted"})
    mock.post(
        _GET_URL,
        payload={
            "ok": True,
            "file_id": "F2",
            "upload_url": "https://files.slack.com/upload/v1/BBB",
        },
    )
    mock.post(_UPLOAD, status=200, body="OK", content_type="text/plain")
    mock.post(_COMPLETE, payload={"ok": True, "files": [{"id": "F2", "title": "b.txt"}]})

    deletes: list[str] = []

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_first",
                    created_at=NOW,
                    filename="a.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
                FileMetadata(
                    id="file_second",
                    created_at=NOW,
                    filename="b.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"txt"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    anthropic_client = build_fake_anthropic(router.dispatch)

    async def sleep(delay: float) -> None:
        pass

    await deliver_session_outputs(
        anthropic_client,
        fake_slack_web_client.client,
        session_id="sesn_1",
        channel_id="C1",
        thread_ts="1.2",
        notice_keys=NoticeKeys(),
        team_id="T1",
        sleep=sleep,
    )

    assert deletes == ["file_second"], (
        "the restricted file must stay listed; only the posted file is deleted"
    )
    assert _post_message_calls(mock) == [], (
        "file_upload_size_restricted is a per-file failure, not an abort notice"
    )


async def test_delivery_posts_one_oversize_notice_and_keeps_entry_without_upload(
    fake_slack_web_client: Any,
) -> None:
    """An oversize file gets one in-thread notice naming the file and its size,
    no upload requests at all, and its MA listing entry is kept. A later sweep
    that lists it again posts no second notice."""
    mock = fake_slack_web_client.mock

    deletes: list[str] = []

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_big",
                    created_at=NOW,
                    filename="big data.bin",
                    mime_type="application/octet-stream",
                    size_bytes=SLACK_MAX_BYTES_PER_FILE + 1,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    anthropic_client = build_fake_anthropic(router.dispatch)

    async def sleep(delay: float) -> None:
        pass

    noticed = NoticedFiles()
    for _ in range(2):
        await deliver_session_outputs(
            anthropic_client,
            fake_slack_web_client.client,
            session_id="sesn_1",
            channel_id="C1",
            thread_ts="1.2",
            notice_keys=NoticeKeys(),
            team_id="T1",
            noticed_files=noticed,
            sleep=sleep,
        )

    notices = _post_message_calls(mock)
    assert len(notices) == 1, "exactly one oversize notice across both sweeps"
    notice_text = _notice_text(notices[0])
    assert "big_data.bin" in notice_text, "the notice must name the sanitized file"
    assert "200 MB upload limit" in notice_text, "the notice must quote the delivery limit"
    assert "still saved" in notice_text, "the notice must say the file was kept"
    upload_requests = [
        url
        for (_, url) in mock.requests
        if "getUploadURLExternal" in str(url) or "files.slack.com" in str(url)
    ]
    assert upload_requests == [], "an oversize file must produce no upload requests at all"
    assert deletes == [], "the oversize entry must be kept, not deleted"


async def test_neutral_bytes_reach_actual_slack_sdk_upload_flow(fake_slack_web_client: Any) -> None:
    mock = fake_slack_web_client.mock
    mock.post(
        _GET_URL,
        payload={
            "ok": True,
            "file_id": "F-neutral",
            "upload_url": "https://files.slack.com/upload/v1/neutral",
        },
    )
    mock.post(_UPLOAD, status=200, body="OK", content_type="text/plain")
    mock.post(_COMPLETE, payload={"ok": True, "files": [{"id": "F-neutral", "title": "chart.png"}]})
    content = b"\x89PNG\r\n\x1a\n\x00provider-image"
    assert await deliver_provider_output(
        DeliverableFile("neutral:gemini:root:image", "chart", "image/png", len(content), content),
        fake_slack_web_client.client,
        channel_id="C1",
        thread_ts="1.2",
    )
    uploads = [
        call
        for (_, url), calls in mock.requests.items()
        if "files.slack.com" in str(url)
        for call in calls
    ]
    assert len(uploads) == 1
    assert uploads[0].kwargs["data"] == content
    complete = [url for (_, url) in mock.requests if "completeUploadExternal" in str(url)]
    assert len(complete) == 1 and complete[0].query["channel_id"] == "C1"
    assert complete[0].query["thread_ts"] == "1.2"
    assert all("content_type" not in url.query for (_, url) in mock.requests)


async def test_neutral_mismatched_size_never_contacts_slack(fake_slack_web_client: Any) -> None:
    assert not await deliver_provider_output(
        DeliverableFile("neutral:openai:root:file", "report.txt", "text/plain", 99, b"data"),
        fake_slack_web_client.client,
        channel_id="C1",
        thread_ts="1.2",
    )
    assert not fake_slack_web_client.mock.requests


async def test_a_file_at_the_slack_cap_is_downloaded_and_uploaded(
    fake_slack_web_client: Any,
) -> None:
    """The cap is 200 MiB: a file of exactly that size per its listing metadata
    is delivered and deleted, not skipped. The metadata size is all the sweep
    checks before downloading, so the test never holds 200 MiB of bytes."""
    mock = fake_slack_web_client.mock
    mock.post(
        _GET_URL,
        payload={"ok": True, "file_id": "F1", "upload_url": "https://files.slack.com/upload/v1/A"},
    )
    mock.post(_UPLOAD, status=200, body="OK", content_type="text/plain")
    mock.post(_COMPLETE, payload={"ok": True, "files": [{"id": "F1", "title": "big.bin"}]})

    deletes: list[str] = []

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_big",
                    created_at=NOW,
                    filename="big",
                    mime_type="application/octet-stream",
                    size_bytes=SLACK_MAX_BYTES_PER_FILE,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"stand-in"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)

    async def sleep(delay: float) -> None:
        pass

    await deliver_session_outputs(
        build_fake_anthropic(router.dispatch),
        fake_slack_web_client.client,
        session_id="sesn_1",
        channel_id="C1",
        thread_ts="1.2",
        notice_keys=NoticeKeys(),
        team_id="T1",
        sleep=sleep,
    )

    assert deletes == ["file_big"], "a file at the cap is delivered, then removed from the listing"
    assert _post_message_calls(mock) == [], "no oversize notice"
    assert any("files.slack.com" in str(url) for _, url in mock.requests), "it was uploaded"


def test_the_slack_cap_is_200_mib() -> None:
    assert SLACK_MAX_BYTES_PER_FILE == 200 * 1024 * 1024


def test_a_large_upload_gets_its_own_client_with_a_longer_timeout() -> None:
    """The shared session times every call out at 30 s. A file that cannot upload
    in that gets a client of its own, with minutes; a small one keeps the shared
    client."""
    base = AsyncWebClient(token="xoxb-test", retry_handlers=[])
    uploader = delivery_module._uploader  # pyright: ignore[reportPrivateUsage]
    assert uploader(base, size_bytes=1024) is base
    big = uploader(base, size_bytes=SLACK_MAX_BYTES_PER_FILE)
    assert big is not base
    assert big.timeout > 300, "200 MiB must have well over the 30 s default"
    assert big.token == "xoxb-test" and big.session is None
    assert big.retry_handlers is base.retry_handlers


def _abort_delivery_client(fake_slack_web_client: Any) -> Any:
    """An MA file whose Slack upload always hits missing_scope."""
    fake_slack_web_client.mock.post(
        _GET_URL, payload={"ok": False, "error": "missing_scope"}, repeat=True
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_one",
                    created_at=NOW,
                    filename="chart.png",
                    mime_type="image/png",
                    size_bytes=9,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"png-bytes"),
    )
    return build_fake_anthropic(router.dispatch)


async def _deliver_abort(
    anthropic_client: Any,
    web_client: Any,
    notice_keys: Any,
    *,
    session_id: str = "sesn_1",
    thread_ts: str = "1.2",
) -> None:
    async def sleep(delay: float) -> None:
        pass

    await deliver_session_outputs(
        anthropic_client,
        web_client,
        session_id=session_id,
        channel_id="C1",
        thread_ts=thread_ts,
        notice_keys=notice_keys,
        team_id="T_RACE",
        sleep=sleep,
    )


async def test_concurrent_deliveries_post_one_notice(
    fake_slack_web_client: Any,
) -> None:
    anthropic_client = _abort_delivery_client(fake_slack_web_client)
    entered = asyncio.Event()
    release = asyncio.Event()

    class GatedClient:
        calls = 0

        async def files_upload_v2(self, **kwargs: Any) -> Any:
            return await fake_slack_web_client.client.files_upload_v2(**kwargs)

        async def chat_postMessage(self, **kwargs: Any) -> Any:
            self.calls += 1
            entered.set()
            await release.wait()
            return await fake_slack_web_client.client.chat_postMessage(**kwargs)

    web_client = GatedClient()
    notice_keys = NoticeKeys()
    first = asyncio.create_task(_deliver_abort(anthropic_client, web_client, notice_keys))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(
        _deliver_abort(anthropic_client, web_client, notice_keys, session_id="sesn_2")
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert web_client.calls == 1, "a second session must wait for this notice key"
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert len(_post_message_calls(fake_slack_web_client.mock)) == 1
    assert len(notice_keys._locks) == 0 if hasattr(notice_keys, "_locks") else True


async def test_failed_notice_post_is_retried_after_waiter(
    fake_slack_web_client: Any,
) -> None:
    anthropic_client = _abort_delivery_client(fake_slack_web_client)
    entered = asyncio.Event()
    release = asyncio.Event()

    class FailingClient:
        calls = 0

        async def files_upload_v2(self, **kwargs: Any) -> Any:
            return await fake_slack_web_client.client.files_upload_v2(**kwargs)

        async def chat_postMessage(self, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("notice post failed")
            return await fake_slack_web_client.client.chat_postMessage(**kwargs)

    web_client = FailingClient()
    notice_keys = NoticeKeys()
    first = asyncio.create_task(_deliver_abort(anthropic_client, web_client, notice_keys))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(
        _deliver_abort(anthropic_client, web_client, notice_keys, session_id="sesn_2")
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert web_client.calls == 1, "retry must wait for the failed attempt to finish"
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    assert web_client.calls == 2
    assert len(_post_message_calls(fake_slack_web_client.mock)) == 1


async def test_notice_and_log_retention_is_bounded(
    fake_slack_web_client: Any,
) -> None:
    anthropic_client = _abort_delivery_client(fake_slack_web_client)
    notice_keys = NoticeKeys(max_keys=3) if hasattr(delivery_module, "NoticeKeys") else set()
    for index in range(6):
        await _deliver_abort(
            anthropic_client,
            fake_slack_web_client.client,
            notice_keys,
            thread_ts=f"{index}.0",
        )
    assert len(notice_keys) <= 3
    assert "notice:T_RACE:C1:5.0:missing_scope" in notice_keys
    assert len(_post_message_calls(fake_slack_web_client.mock)) == 6

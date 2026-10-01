"""Tests for the Slack output-delivery adapter.

MA traffic runs through MARouter + build_fake_anthropic (httpx transport);
Slack traffic runs through the aioresponses-backed ``fake_slack_web_client``
fixture — the real slack_sdk request builder and response parser run for
every call. No method-level mocks anywhere.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
from anthropic.types.beta import FileMetadata
from daimon.adapters.slack.output_delivery import deliver_session_outputs
from daimon.core.output_delivery import MAX_BYTES_PER_FILE
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from yarl import URL

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)

_GET_URL = re.compile(r"https://slack\.com/api/files\.getUploadURLExternal.*")
_UPLOAD = re.compile(r"https://files\.slack\.com/upload/v1/.*")
_COMPLETE = re.compile(r"https://slack\.com/api/files\.completeUploadExternal.*")
_POST_MESSAGE_KEY = ("POST", URL("https://slack.com/api/chat.postMessage"))


def _post_message_calls(mock: Any) -> list[Any]:
    return list(mock.requests.get(_POST_MESSAGE_KEY, []))


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
        notice_keys=set(),
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
    """missing_scope aborts the sweep: one actionable notice naming files:write,
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

    notice_keys: set[str] = set()
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
    assert "files:write" in str(notices[0].kwargs), "the notice must name the missing scope"
    assert "reinstall" in str(notices[0].kwargs), "the notice must tell the admin to reinstall"
    assert deletes == [], "an aborted sweep must delete nothing"
    assert "T_SCOPE:missing_scope" in notice_keys, (
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

    notice_keys: set[str] = set()
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
    notice_text = str(notices[0].kwargs)
    assert "file-storage limit" in notice_text, (
        "the storage notice must be about the workspace being out of file storage"
    )
    assert "scope" not in notice_text, "the storage notice must not talk about scopes"
    assert deletes == [], "an aborted sweep must delete nothing"
    assert "T_STORE:storage_limit_reached" in notice_keys, (
        "the dedup key must be recorded after the notice posts"
    )


async def test_delivery_posts_abort_notice_once_per_team_per_code(
    fake_slack_web_client: Any,
) -> None:
    """A second delivery under the same abort code and team posts no second
    notice — the shared notice_keys set dedups per team per code."""
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

    notice_keys: set[str] = set()
    for _ in range(2):
        await deliver_session_outputs(
            anthropic_client,
            fake_slack_web_client.client,
            session_id="sesn_1",
            channel_id="C1",
            thread_ts="1.2",
            notice_keys=notice_keys,
            team_id="T_ONCE",
            sleep=sleep,
        )

    notices = _post_message_calls(mock)
    assert len(notices) == 1, "the abort notice must be posted once per team per code"


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
        notice_keys=set(),
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
        notice_keys=set(),
        team_id="T1",
        sleep=sleep,
    )

    assert deletes == ["file_second"], (
        "the restricted file must stay listed; only the posted file is deleted"
    )
    assert _post_message_calls(mock) == [], (
        "file_upload_size_restricted is a per-file failure, not an abort notice"
    )


async def test_delivery_posts_oversize_notice_and_deletes_entry_without_upload(
    fake_slack_web_client: Any,
) -> None:
    """An oversize file gets one in-thread notice naming the file and its size,
    no upload requests at all, and its MA listing entry is deleted."""
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
                    size_bytes=MAX_BYTES_PER_FILE + 1,
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

    await deliver_session_outputs(
        anthropic_client,
        fake_slack_web_client.client,
        session_id="sesn_1",
        channel_id="C1",
        thread_ts="1.2",
        notice_keys=set(),
        team_id="T1",
        sleep=sleep,
    )

    notices = _post_message_calls(mock)
    assert len(notices) == 1, "exactly one oversize notice must be posted"
    notice_text = str(notices[0].kwargs)
    assert "big_data.bin" in notice_text, "the notice must name the sanitized file"
    assert "20 MiB" in notice_text, "the notice must quote the delivery limit"
    upload_requests = [
        url
        for (_, url) in mock.requests
        if "getUploadURLExternal" in str(url) or "files.slack.com" in str(url)
    ]
    assert upload_requests == [], "an oversize file must produce no upload requests at all"
    assert deletes == ["file_big"], "the oversize entry must be deleted after the notice"

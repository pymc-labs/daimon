"""Tests for daimon.core.output_delivery — the post-then-delete sweep engine.

All MA traffic runs transport-level through MARouter + build_fake_anthropic:
the real SDK parses every request and response. The sweep's sleep is injected
as a recording no-op so each test can assert the exact poll cadence the
settle floor produces.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from anthropic.types.beta import FileMetadata
from daimon.core.output_delivery import (
    DeliverableFile,
    OutputDeliveryDeferred,
    OutputPostingUnavailable,
    SkippedFile,
    render_oversize_notice,
    sweep_session_outputs,
)
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


async def test_sweep_posts_file_when_it_first_appears_on_second_poll() -> None:
    """Indexing lag: an empty first poll must not conclude the sweep; the file
    arriving on the t=2 poll is posted after the settle floor's confirming poll."""
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    polls = 0

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        nonlocal polls
        polls += 1
        if polls == 1:
            return list_response([])
        return list_response(
            [
                FileMetadata(
                    id="file_late",
                    created_at=NOW,
                    filename="report.csv",
                    mime_type="text/plain",
                    size_bytes=4,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        )

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"data"),
    )
    router.add(
        "DELETE",
        r"/v1/files/([^/]+)",
        lambda request, match: httpx.Response(
            200, json={"id": match.group(1), "type": "file_deleted"}
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    posted: list[DeliverableFile] = []

    async def post(file: DeliverableFile) -> None:
        posted.append(file)

    count = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert count == 1, "the late-indexed file should be posted"
    assert [f.file_id for f in posted] == ["file_late"], "posted file must be the listed one"
    assert delays == [2.0, 4.0], "settle floor must force the confirming t=6 poll before concluding"


async def test_sweep_catches_straggler_when_new_file_appears_at_settle_floor() -> None:
    """A file first appearing in the t=6 poll extends the schedule to t=14 and
    both files are posted."""
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    polls = 0

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        nonlocal polls
        polls += 1
        rows = [
            FileMetadata(
                id="file_a",
                created_at=NOW,
                filename="early.txt",
                mime_type="text/plain",
                size_bytes=5,
                type="file",
                downloadable=True,
            ).model_dump(mode="json")
        ]
        if polls >= 3:
            rows.append(
                FileMetadata(
                    id="file_b",
                    created_at=NOW,
                    filename="late.txt",
                    mime_type="text/plain",
                    size_bytes=5,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            )
        return list_response(rows)

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"bytes"),
    )
    router.add(
        "DELETE",
        r"/v1/files/([^/]+)",
        lambda request, match: httpx.Response(
            200, json={"id": match.group(1), "type": "file_deleted"}
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    posted: list[str] = []

    async def post(file: DeliverableFile) -> None:
        posted.append(file.file_id)

    count = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert count == 2, "both the early file and the t=6 straggler should be posted"
    assert sorted(posted) == ["file_a", "file_b"], "both listed files must be posted"
    assert delays == [2.0, 4.0, 8.0], (
        "a new id at the t=6 poll must extend the schedule to the t=14 poll"
    )


async def test_sweep_returns_zero_when_every_poll_is_empty() -> None:
    """All-empty listings give up at the t=6 poll — never burning the t=14 poll."""
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    router = MARouter()
    router.add("GET", r"/v1/files", lambda request, match: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    async def post(file: DeliverableFile) -> None:
        raise AssertionError("nothing should be posted from an empty listing")

    count = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert count == 0, "an all-empty run posts nothing"
    assert delays == [2.0, 4.0], "empty sweep must give up at the t=6 poll, never reach t=14"


async def test_sweep_never_downloads_entry_when_downloadable_is_not_true() -> None:
    """The mounted .env credential entry (downloadable=False) is never
    downloaded and never posted — the hard security gate."""

    async def sleep(delay: float) -> None:
        pass

    downloads: list[str] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_env",
                    created_at=NOW,
                    filename=".env",
                    mime_type="text/plain",
                    size_bytes=128,
                    type="file",
                    downloadable=False,
                ).model_dump(mode="json"),
                FileMetadata(
                    id="file_chart",
                    created_at=NOW,
                    filename="chart.png",
                    mime_type="image/png",
                    size_bytes=64,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
            ]
        )

    def on_download(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        downloads.append(match.group(1))
        return httpx.Response(200, content=b"png-bytes")

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add("GET", r"/v1/files/([^/]+)/content", on_download)
    router.add(
        "DELETE",
        r"/v1/files/([^/]+)",
        lambda request, match: httpx.Response(
            200, json={"id": match.group(1), "type": "file_deleted"}
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    posted: list[str] = []

    async def post(file: DeliverableFile) -> None:
        posted.append(file.file_id)

    count = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert count == 1, "only the downloadable entry should be posted"
    assert posted == ["file_chart"], "the non-downloadable .env entry must never be posted"
    assert downloads == ["file_chart"], (
        "the download route must never be hit for the non-downloadable .env id"
    )


async def test_sweep_posts_before_deleting_and_deleted_entry_leaves_listing() -> None:
    """post-then-delete ordering: every post precedes its delete, and a deleted
    entry no longer appears in the next sweep's listing."""

    async def sleep(delay: float) -> None:
        pass

    events: list[str] = []
    deleted: set[str] = set()

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        rows = [
            FileMetadata(
                id="file_a",
                created_at=NOW,
                filename="a.txt",
                mime_type="text/plain",
                size_bytes=3,
                type="file",
                downloadable=True,
            ),
            FileMetadata(
                id="file_b",
                created_at=NOW,
                filename="b.txt",
                mime_type="text/plain",
                size_bytes=3,
                type="file",
                downloadable=True,
            ),
        ]
        return list_response([row.model_dump(mode="json") for row in rows if row.id not in deleted])

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        file_id = match.group(1)
        deleted.add(file_id)
        events.append(f"delete:{file_id}")
        return httpx.Response(200, json={"id": file_id, "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"txt"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    async def post(file: DeliverableFile) -> None:
        events.append(f"post:{file.file_id}")

    first = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert first == 2, "both files should be posted on the first sweep"
    for file_id in ("file_a", "file_b"):
        assert events.index(f"post:{file_id}") < events.index(f"delete:{file_id}"), (
            f"{file_id} must be posted before its listing entry is deleted"
        )

    second = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert second == 0, "a deleted entry must not appear in the next sweep's listing"


async def test_sweep_counts_post_when_delete_fails_afterwards() -> None:
    """A DELETE failure after a successful post does not undo the post and does
    not stop the sweep — both files count."""

    async def sleep(delay: float) -> None:
        pass

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_a",
                    created_at=NOW,
                    filename="a.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
                FileMetadata(
                    id="file_b",
                    created_at=NOW,
                    filename="b.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
            ]
        )

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        file_id = match.group(1)
        if file_id == "file_a":
            return httpx.Response(
                500,
                json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
            )
        return httpx.Response(200, json={"id": file_id, "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"txt"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch).with_options(max_retries=0)

    posted: list[str] = []

    async def post(file: DeliverableFile) -> None:
        posted.append(file.file_id)

    count = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert count == 2, "a delete failure after a successful post must still count the post"
    assert posted == ["file_a", "file_b"], "the sweep must continue past the failed delete"


async def test_sweep_skips_oversize_file_with_notice_then_deletes_entry() -> None:
    """An oversize entry is never downloaded, produces one on_skip call, and its
    listing entry is deleted so it never re-lists forever."""

    async def sleep(delay: float) -> None:
        pass

    downloads: list[str] = []
    deletes: list[str] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_big",
                    created_at=NOW,
                    filename="huge.parquet",
                    mime_type="application/octet-stream",
                    size_bytes=11,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        )

    def on_download(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        downloads.append(match.group(1))
        return httpx.Response(200, content=b"x")

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add("GET", r"/v1/files/([^/]+)/content", on_download)
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    skipped: list[SkippedFile] = []

    async def on_skip(file: SkippedFile) -> None:
        skipped.append(file)

    async def post(file: DeliverableFile) -> None:
        raise AssertionError("an oversize file must never be posted")

    count = await sweep_session_outputs(
        client, session_id="sesn_1", post=post, on_skip=on_skip, sleep=sleep, max_bytes=10
    )

    assert count == 0, "an oversize file does not count as posted"
    assert len(skipped) == 1, "on_skip must be called exactly once for the oversize file"
    assert skipped[0] == SkippedFile(file_id="file_big", filename="huge.parquet", size_bytes=11), (
        "the SkippedFile must carry the oversize entry's id, filename and size"
    )
    assert downloads == [], "the oversize file's bytes must never be downloaded"
    assert deletes == ["file_big"], "the oversize entry must be deleted after the notice"


async def test_sweep_keeps_oversize_entry_when_skip_notice_fails() -> None:
    """If on_skip raises, the oversize entry is NOT deleted (the notice can retry
    next sweep) and a following normal file is still delivered."""

    async def sleep(delay: float) -> None:
        pass

    deletes: list[str] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_big",
                    created_at=NOW,
                    filename="huge.bin",
                    mime_type="application/octet-stream",
                    size_bytes=11,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
                FileMetadata(
                    id="file_ok",
                    created_at=NOW,
                    filename="ok.txt",
                    mime_type="text/plain",
                    size_bytes=2,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
            ]
        )

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"ok"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    async def on_skip(file: SkippedFile) -> None:
        raise RuntimeError("notice channel is down")

    posted: list[str] = []

    async def post(file: DeliverableFile) -> None:
        posted.append(file.file_id)

    count = await sweep_session_outputs(
        client, session_id="sesn_1", post=post, on_skip=on_skip, sleep=sleep, max_bytes=10
    )

    assert count == 1, "the normal file must still be posted after the failed skip notice"
    assert posted == ["file_ok"], "only the normal file is posted"
    assert deletes == ["file_ok"], (
        "the oversize entry must NOT be deleted when its skip notice failed"
    )


async def test_sweep_deletes_zero_byte_entry_without_download_or_notice() -> None:
    """A 0-byte entry is deleted with no download and no on_skip call."""

    async def sleep(delay: float) -> None:
        pass

    downloads: list[str] = []
    deletes: list[str] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_empty",
                    created_at=NOW,
                    filename="empty.txt",
                    mime_type="text/plain",
                    size_bytes=0,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        )

    def on_download(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        downloads.append(match.group(1))
        return httpx.Response(200, content=b"")

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add("GET", r"/v1/files/([^/]+)/content", on_download)
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    skips: list[SkippedFile] = []

    async def on_skip(file: SkippedFile) -> None:
        skips.append(file)

    async def post(file: DeliverableFile) -> None:
        raise AssertionError("a 0-byte file must never be posted")

    count = await sweep_session_outputs(
        client, session_id="sesn_1", post=post, on_skip=on_skip, sleep=sleep
    )

    assert count == 0, "a 0-byte entry does not count as posted"
    assert downloads == [], "a 0-byte entry must never be downloaded"
    assert skips == [], "on_skip must not be called for a 0-byte entry"
    assert deletes == ["file_empty"], "the 0-byte entry must be deleted"


async def test_sweep_continues_when_post_fails_for_one_file() -> None:
    """A post failure isolates to that file: it stays listed, the next file is
    posted and deleted, and the return value counts only the success."""

    async def sleep(delay: float) -> None:
        pass

    deletes: list[str] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_bad",
                    created_at=NOW,
                    filename="bad.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
                FileMetadata(
                    id="file_good",
                    created_at=NOW,
                    filename="good.txt",
                    mime_type="text/plain",
                    size_bytes=4,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
            ]
        )

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"txt"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    async def post(file: DeliverableFile) -> None:
        if file.file_id == "file_bad":
            raise RuntimeError("upload exploded")

    count = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert count == 1, "only the successfully posted file counts"
    assert deletes == ["file_good"], (
        "the failed file must stay listed; only the posted file is deleted"
    )


async def test_sweep_leaves_deferred_file_listed_without_counting_it() -> None:
    """A poster that defers (a consent prompt) keeps the file listed for the
    later step to delete, and the sweep carries on with the next file."""

    async def sleep(delay: float) -> None:
        pass

    deletes: list[str] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_offered",
                    created_at=NOW,
                    filename="offered.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
                FileMetadata(
                    id="file_posted",
                    created_at=NOW,
                    filename="posted.txt",
                    mime_type="text/plain",
                    size_bytes=4,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
            ]
        )

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"txt"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    async def post(file: DeliverableFile) -> None:
        if file.file_id == "file_offered":
            raise OutputDeliveryDeferred(file.file_id)

    count = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert count == 1, "a deferred file is not counted as posted"
    assert deletes == ["file_posted"], "the deferred file must stay listed"


def test_oversize_notice_names_the_file_and_the_limit() -> None:
    """The notice sanitizes the agent-supplied name and quotes both sizes."""
    notice = render_oversize_notice(
        SkippedFile(file_id="file_big", filename="big report.csv", size_bytes=25 * 1024 * 1024)
    )
    assert notice == (
        "I couldn't attach `big_report.csv` — it is 25.0 MiB, over the 20 MiB delivery limit."
    ), "notice text"


async def test_sweep_aborts_and_deletes_nothing_when_posting_unavailable() -> None:
    """OutputPostingUnavailable propagates out of the sweep: no DELETE is ever
    made and no further file is downloaded."""

    async def sleep(delay: float) -> None:
        pass

    downloads: list[str] = []
    deletes: list[str] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_first",
                    created_at=NOW,
                    filename="first.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
                FileMetadata(
                    id="file_second",
                    created_at=NOW,
                    filename="second.txt",
                    mime_type="text/plain",
                    size_bytes=3,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json"),
            ]
        )

    def on_download(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        downloads.append(match.group(1))
        return httpx.Response(200, content=b"txt")

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add("GET", r"/v1/files/([^/]+)/content", on_download)
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    async def post(file: DeliverableFile) -> None:
        raise OutputPostingUnavailable("missing_scope")

    with pytest.raises(OutputPostingUnavailable):
        await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert deletes == [], "an aborted sweep must delete nothing"
    assert downloads == ["file_first"], "the second file must never be downloaded after the abort"


async def test_sweep_skips_handoff_bundles_and_leaves_them_listed() -> None:
    """A workspace-transfer bundle shares the outputs directory with real user
    files but belongs to `daimon.core.workspace_transfer`, which downloads and
    deletes it itself. Posting it would hand the user a tarball they never
    asked for; deleting it would destroy the handoff. So the sweep does
    neither, and the entry is still listed afterwards."""

    async def sleep(delay: float) -> None:
        pass

    deleted: set[str] = set()

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        rows = [
            FileMetadata(
                id="file_bundle",
                created_at=NOW,
                filename="daimon-handoff-0f9d1c4e.tar.gz",
                mime_type="application/gzip",
                size_bytes=2048,
                type="file",
                downloadable=True,
            ),
            FileMetadata(
                id="file_report",
                created_at=NOW,
                filename="report.csv",
                mime_type="text/csv",
                size_bytes=3,
                type="file",
                downloadable=True,
            ),
        ]
        return list_response([row.model_dump(mode="json") for row in rows if row.id not in deleted])

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deleted.add(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    downloaded: list[str] = []

    def on_download(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        downloaded.append(match.group(1))
        return httpx.Response(200, content=b"csv")

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add("GET", r"/v1/files/([^/]+)/content", on_download)
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    posted: list[str] = []

    async def post(file: DeliverableFile) -> None:
        posted.append(file.filename)

    count = await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep)

    assert count == 1, "only the real output counts as posted"
    assert posted == ["report.csv"], "the handoff bundle must never be posted"
    assert downloaded == ["file_report"], "an excluded entry is not even downloaded"
    assert deleted == {"file_report"}, "the handoff bundle must survive the sweep"

    listing = await client.beta.files.list(scope_id="sesn_1", limit=1000)
    assert [meta.id for meta in listing.data] == ["file_bundle"], (
        "the skipped bundle is still listed for its owner to collect"
    )


async def test_sweep_posts_handoff_bundle_when_exclusions_are_cleared() -> None:
    """The exclusion is a parameter, not a hard-coded name filter: a caller
    that passes no prefixes sweeps the bundle like any other output."""

    async def sleep(delay: float) -> None:
        pass

    deleted: set[str] = set()

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        row = FileMetadata(
            id="file_bundle",
            created_at=NOW,
            filename="daimon-handoff-0f9d1c4e.tar.gz",
            mime_type="application/gzip",
            size_bytes=4,
            type="file",
            downloadable=True,
        )
        return list_response([] if row.id in deleted else [row.model_dump(mode="json")])

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deleted.add(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"gzip"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    posted: list[str] = []

    async def post(file: DeliverableFile) -> None:
        posted.append(file.filename)

    count = await sweep_session_outputs(
        client,
        session_id="sesn_1",
        post=post,
        sleep=sleep,
        exclude_filename_prefixes=(),
    )

    assert count == 1, "with no exclusions the bundle is an ordinary output"
    assert posted == ["daimon-handoff-0f9d1c4e.tar.gz"]
    assert deleted == {"file_bundle"}


async def test_sweep_leaves_a_later_turns_files_listed() -> None:
    """The listing is session-wide and the next turn may be writing while this
    sweep settles. With ``created_before`` the sweep takes only files made
    before its turn ended and leaves the rest, untouched, for the next sweep,
    including an empty or oversize one it would otherwise clear."""

    async def sleep(delay: float) -> None:
        pass

    deleted: set[str] = set()
    later = NOW + timedelta(seconds=1)

    def row(file_id: str, created_at: datetime, size_bytes: int) -> FileMetadata:
        return FileMetadata(
            id=file_id,
            created_at=created_at,
            filename=f"{file_id}.csv",
            mime_type="text/csv",
            size_bytes=size_bytes,
            type="file",
            downloadable=True,
        )

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        rows = [
            row("mine", NOW - timedelta(seconds=1), 3),
            row("at_the_end", NOW, 3),
            row("next_turn", later, 3),
            row("next_turn_empty", later, 0),
        ]
        return list_response([r.model_dump(mode="json") for r in rows if r.id not in deleted])

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deleted.add(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"csv"),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)

    posted: list[str] = []

    async def post(file: DeliverableFile) -> None:
        posted.append(file.file_id)

    count = await sweep_session_outputs(
        client, session_id="sesn_1", post=post, sleep=sleep, created_before=NOW
    )

    assert count == 1 and posted == ["mine"], "only this turn's file is delivered"
    assert deleted == {"mine"}, "a later turn's entries are neither posted nor deleted"

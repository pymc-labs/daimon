"""Tests for daimon.core.output_delivery — the post-then-delete sweep engine.

All MA traffic runs transport-level through MARouter + build_fake_anthropic:
the real SDK parses every request and response. The sweep's sleep is injected
as a recording no-op so each test can assert the exact poll cadence the
settle floor produces.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.core.output_delivery import (
    DeliverableFile,
    NoticedFiles,
    OutputDeliveryDeferred,
    OutputPostingUnavailable,
    SkippedFile,
    download_output_file,
    render_oversize_notice,
    sweep_session_outputs,
)
from daimon.testing.ma import (
    FakeMAState,
    MARouter,
    build_fake_anthropic,
    combine_handlers,
    list_response,
)
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler

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


async def test_sweep_skips_oversize_file_with_notice_and_keeps_entry() -> None:
    """An oversize entry is never downloaded, produces one on_skip call, and its
    listing entry is kept: deleting it would lose the person's file."""

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
    assert deletes == [], "the oversize entry must be kept after the notice"


def _oversize_then_normal_client(
    deletes: list[str],
) -> AsyncAnthropic:
    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id="file_big",
                    created_at=NOW,
                    filename="render.mp4",
                    mime_type="video/mp4",
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
    return build_fake_anthropic(router.dispatch)


async def test_a_kept_oversize_file_is_announced_once_across_sweeps() -> None:
    """The kept entry lists again on every later sweep; ``noticed`` stops a
    repeat notice, while a normal file is still posted and deleted."""

    async def sleep(delay: float) -> None:
        pass

    deletes: list[str] = []
    client = _oversize_then_normal_client(deletes)
    skipped: list[str] = []
    posted: list[str] = []

    async def on_skip(file: SkippedFile) -> None:
        skipped.append(file.file_id)

    async def post(file: DeliverableFile) -> None:
        posted.append(file.file_id)

    noticed = NoticedFiles()
    for _ in range(2):
        await sweep_session_outputs(
            client,
            session_id="sesn_1",
            post=post,
            on_skip=on_skip,
            sleep=sleep,
            max_bytes=10,
            noticed=noticed,
        )

    assert skipped == ["file_big"], "one notice for the kept file, not one per sweep"
    assert "file_big" in noticed
    assert posted == ["file_ok", "file_ok"], "the fake keeps listing it; each sweep posts it"
    assert "file_big" not in deletes, "the oversize entry is never deleted"
    assert deletes == ["file_ok", "file_ok"], "a delivered file is still cleaned up"


async def test_without_a_ledger_every_sweep_repeats_the_notice() -> None:
    async def sleep(delay: float) -> None:
        pass

    client = _oversize_then_normal_client([])
    skipped: list[str] = []

    async def on_skip(file: SkippedFile) -> None:
        skipped.append(file.file_id)

    async def post(file: DeliverableFile) -> None:
        pass

    for _ in range(2):
        await sweep_session_outputs(
            client, session_id="sesn_1", post=post, on_skip=on_skip, sleep=sleep, max_bytes=10
        )

    assert skipped == ["file_big", "file_big"]


async def test_a_failed_notice_is_not_recorded_and_retries() -> None:
    async def sleep(delay: float) -> None:
        pass

    client = _oversize_then_normal_client([])
    calls = 0

    async def on_skip(file: SkippedFile) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("chat is down")

    async def post(file: DeliverableFile) -> None:
        pass

    noticed = NoticedFiles()
    for _ in range(3):
        await sweep_session_outputs(
            client,
            session_id="sesn_1",
            post=post,
            on_skip=on_skip,
            sleep=sleep,
            max_bytes=10,
            noticed=noticed,
        )

    assert calls == 2, "the failed notice retries once, then is recorded"


async def test_discard_skipped_up_to_deletes_only_files_that_fit() -> None:
    """For a caller that can deliver nothing right now (``max_bytes=0``): a
    file under the real cap is discarded as before, a larger one is kept."""

    async def sleep(delay: float) -> None:
        pass

    deletes: list[str] = []
    client = _oversize_then_normal_client(deletes)  # file_big is 11 bytes, file_ok 2

    async def post(file: DeliverableFile) -> None:
        raise AssertionError("nothing is posted when max_bytes is 0")

    await sweep_session_outputs(
        client, session_id="sesn_1", post=post, sleep=sleep, max_bytes=0, discard_skipped_up_to=10
    )

    assert deletes == ["file_ok"], "only the file that fits the cap is discarded"


async def test_a_skipped_file_carries_its_mime_type() -> None:
    """A caller that skips everything can still tell an image from the rest."""

    async def sleep(delay: float) -> None:
        pass

    deletes: list[str] = []
    skipped: list[SkippedFile] = []

    async def post(file: DeliverableFile) -> None:
        raise AssertionError("a zero cap posts nothing")

    async def on_skip(file: SkippedFile) -> None:
        skipped.append(file)

    await sweep_session_outputs(
        _oversize_then_normal_client(deletes),
        session_id="sesn_1",
        post=post,
        on_skip=on_skip,
        sleep=sleep,
        max_bytes=0,
    )

    assert [(f.file_id, f.mime_type) for f in skipped] == [
        ("file_big", "video/mp4"),
        ("file_ok", "text/plain"),
    ], "each skipped file names its type"


async def test_a_kept_original_stays_downloadable_byte_for_byte() -> None:
    """Against the stateful Files API fake: after a failed then retried notice,
    the oversize original is still listed and downloads unchanged, while the
    normal and empty files are cleaned up."""
    state = FakeSessionsState(ma=FakeMAState())
    client = build_fake_anthropic(combine_handlers(make_fake_sessions_handler(state)))
    original = b"original-render-" * 100
    big = state.write_output("sesn_k", "render.mp4", original)
    normal = state.write_output("sesn_k", "poster.txt", b"ok")
    empty = state.write_output("sesn_k", "empty.txt", b"")
    notices: list[str] = []
    posts: list[str] = []
    noticed = NoticedFiles()

    async def sleep(delay: float) -> None:
        pass

    async def on_skip(file: SkippedFile) -> None:
        notices.append(file.file_id)
        if len(notices) == 1:
            raise RuntimeError("notice transport failed")

    async def post(file: DeliverableFile) -> None:
        posts.append(file.file_id)

    for _ in range(3):
        await sweep_session_outputs(
            client,
            session_id="sesn_k",
            post=post,
            on_skip=on_skip,
            noticed=noticed,
            max_bytes=10,
            sleep=sleep,
        )

    assert notices == [big.id, big.id], "the failed notice retries once, then stays quiet"
    assert posts == [normal.id]
    assert normal.id not in state.files and empty.id not in state.files
    assert big.id in state.files
    assert await download_output_file(client, big.id) == original


async def test_kept_files_filling_the_first_page_do_not_hide_a_later_file() -> None:
    """Kept entries never leave the listing; the sweep must follow the cursor."""
    state = FakeSessionsState(ma=FakeMAState())
    client = build_fake_anthropic(combine_handlers(make_fake_sessions_handler(state)))
    for index in range(1000):
        state.write_output("sesn_p", f"big-{index}", b"xx")
    normal = state.write_output("sesn_p", "normal.txt", b"x")
    posts: list[str] = []

    async def sleep(delay: float) -> None:
        pass

    async def on_skip(file: SkippedFile) -> None:
        pass

    async def post(file: DeliverableFile) -> None:
        posts.append(file.file_id)

    await sweep_session_outputs(
        client,
        session_id="sesn_p",
        post=post,
        on_skip=on_skip,
        noticed=NoticedFiles(),
        max_bytes=1,
        sleep=sleep,
    )

    assert posts == [normal.id], "the file on the second page is delivered"
    assert len(state.files) == 1000, "the kept files stay listed"


def test_noticed_files_forgets_the_least_recently_seen_id_first() -> None:
    noticed = NoticedFiles(max_ids=2)
    noticed.add("a")
    noticed.add("b")
    assert "a" in noticed  # refreshes "a"
    noticed.add("c")
    assert "a" in noticed and "c" in noticed and "b" not in noticed
    assert len(noticed) == 2
    with pytest.raises(ValueError):
        NoticedFiles(max_ids=0)


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


def test_oversize_notice_names_the_file_the_limit_and_how_to_get_it() -> None:
    """The notice sanitizes the agent-supplied name, quotes both sizes, says the
    file was kept, and offers only what the agent can do."""
    notice = render_oversize_notice(
        SkippedFile(file_id="file_big", filename="big report.csv", size_bytes=25 * 1024 * 1024)
    )
    assert notice == (
        "`big_report.csv` is 25 MB, over the 20 MB upload limit here, so I couldn't attach "
        "it. It's still saved in my workspace: ask me for a smaller version, or to split it "
        "into parts under 20 MB, and I'll attach those."
    ), "notice text"


def test_oversize_notice_quotes_the_callers_limit() -> None:
    notice = render_oversize_notice(
        SkippedFile(file_id="f", filename="render.mp4", size_bytes=int(182.34 * 1024 * 1024)),
        max_bytes=10 * 1024 * 1024,
    )
    assert "`render.mp4` is 182.3 MB, over the 10 MB upload limit" in notice
    assert "parts under 10 MB" in notice


def test_oversize_notice_quotes_a_200_mib_limit() -> None:
    notice = render_oversize_notice(
        SkippedFile(file_id="f", filename="dump.bin", size_bytes=250 * 1024 * 1024),
        max_bytes=200 * 1024 * 1024,
    )
    assert "`dump.bin` is 250 MB, over the 200 MB upload limit" in notice
    assert "parts under 200 MB" in notice


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


async def test_sweep_posts_in_the_order_the_caller_gives() -> None:
    """`order` maps the listed filenames to the indexes to post them in."""

    async def sleep(delay: float) -> None:
        pass

    names = ["first.pdf", "second.png"]
    listing = [
        FileMetadata(
            id=f"file_{index}",
            created_at=NOW,
            filename=name,
            mime_type="application/octet-stream",
            size_bytes=3,
            type="file",
            downloadable=True,
        ).model_dump(mode="json")
        for index, name in enumerate(names)
    ]
    router = MARouter()
    router.add("GET", r"/v1/files", lambda request, match: list_response(listing))
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=b"x"),
    )
    router.add(
        "DELETE",
        r"/v1/files/([^/]+)",
        lambda request, match: httpx.Response(
            200, json={"id": match.group(1), "type": "file_deleted"}
        ),
    )
    seen: list[list[str]] = []
    posted: list[str] = []

    def reverse(filenames: Sequence[str]) -> list[int]:
        seen.append(list(filenames))
        return [1, 0]

    async def post(file: DeliverableFile) -> None:
        posted.append(file.filename)

    client = build_fake_anthropic(router.dispatch)
    await sweep_session_outputs(client, session_id="sesn_1", post=post, sleep=sleep, order=reverse)

    assert seen == [names], "order sees the listed names"
    assert posted == ["second.png", "first.pdf"], "posted in the order given"

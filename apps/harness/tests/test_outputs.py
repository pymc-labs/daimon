from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import httpx
import pytest
from mux.drivers.selfhosted.wire import OutputsResponse, output_id

from .conftest import FakeModelAdapter, turn

Harness = tuple[httpx.AsyncClient, FakeModelAdapter, Path]
BASE = "/v0/sessions/session-1/outputs"


async def outputs_dir(client: httpx.AsyncClient, home: Path) -> Path:
    (await client.post("/v0/turns", json=turn())).raise_for_status()
    work = home / "sessions" / hashlib.sha256(b"session-1").hexdigest()[:32] / "work"
    (work / "outputs").mkdir()
    return work / "outputs"


async def listed(client: httpx.AsyncClient) -> OutputsResponse:
    response = await client.get(BASE, params={"session_id": "session-1"})
    assert response.status_code == 200, response.text
    return OutputsResponse.model_validate(response.json())


async def test_outputs_when_saved_then_listed_read_and_deleted(harness: Harness) -> None:
    """A saved file is listed, its bytes read back, and deleting it removes it."""
    client, _, home = harness
    outputs = await outputs_dir(client, home)
    (outputs / "nested").mkdir()
    (outputs / "nested" / "report.csv").write_bytes(b"a,b\n1,2\n")
    files = (await listed(client)).outputs
    assert [(f.path, f.size_bytes) for f in files] == [("nested/report.csv", 8)], files
    read = (await client.get(f"{BASE}/{files[0].id}")).json()
    assert base64.b64decode(read["content_b64"]) == b"a,b\n1,2\n", "content must round-trip"
    deleted = await client.post(f"{BASE}/{files[0].id}/delete")
    assert deleted.json() == {"deleted": True, "id": files[0].id}, deleted.text
    assert not (outputs / "nested" / "report.csv").exists(), "delete must unlink the file"
    assert (await client.get(f"{BASE}/{files[0].id}")).status_code == 404, "gone after delete"


async def test_outputs_when_no_workspace_then_empty(harness: Harness) -> None:
    """A session that never ran lists nothing instead of failing."""
    client, _, _ = harness
    assert (await listed(client)).outputs == [], "no workspace means no files"


async def test_outputs_when_links_then_skipped_and_refused(
    harness: Harness, tmp_path: Path
) -> None:
    """Symlinked files and directories and hard links never leave the workspace."""
    client, _, home = harness
    outputs = await outputs_dir(client, home)
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (outputs / "link.txt").symlink_to(secret)
    (outputs / "linked-dir").symlink_to(tmp_path)
    os.link(secret, outputs / "hard.txt")
    (outputs / "ok.txt").write_text("fine")
    assert [f.path for f in (await listed(client)).outputs] == ["ok.txt"], "links must be skipped"
    info = secret.stat()
    for path in ("link.txt", "hard.txt", "linked-dir/secret.txt"):
        forged = output_id(path, f"{info.st_mtime_ns}-{info.st_size}-{info.st_ino}")
        response = await client.get(f"{BASE}/{forged}")
        assert response.status_code in {404, 422}, (path, response.text)
        assert "private" not in response.text, "linked content must never be returned"
        assert (await client.post(f"{BASE}/{forged}/delete")).status_code in {404, 422}, path
    assert secret.exists(), "a link target must never be deleted"


async def test_outputs_when_directory_is_symlink_then_empty(
    harness: Harness, tmp_path: Path
) -> None:
    """An outputs directory replaced by a symlink delivers nothing."""
    client, _, home = harness
    outputs = await outputs_dir(client, home)
    outputs.rmdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret.txt").write_text("private")
    outputs.symlink_to(tmp_path / "elsewhere")
    assert (await listed(client)).outputs == [], "a symlinked outputs directory must be ignored"


@pytest.mark.parametrize("path", ["../escape.txt", "a/../../b", "/etc/passwd", "a//b", "."])
async def test_outputs_when_path_escapes_then_refused(harness: Harness, path: str) -> None:
    """Ids carrying `..`, absolute or empty parts are refused before any file access."""
    client, _, home = harness
    await outputs_dir(client, home)
    response = await client.get(f"{BASE}/{output_id(path, '1-1-1')}")
    assert response.status_code == 422, response.text


async def test_outputs_when_file_changed_then_conflict_and_kept(harness: Harness) -> None:
    """A file rewritten after listing is neither read under the old id nor deleted."""
    client, _, home = harness
    outputs = await outputs_dir(client, home)
    (outputs / "draft.txt").write_text("one")
    [file] = (await listed(client)).outputs
    (outputs / "draft.txt").write_text("two, longer")
    assert (await client.get(f"{BASE}/{file.id}")).status_code == 409, "stale id must not read"
    assert (await client.post(f"{BASE}/{file.id}/delete")).status_code == 409, "stale delete"
    assert (outputs / "draft.txt").read_text() == "two, longer", "rewritten file must survive"


async def test_outputs_when_too_large_then_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over the read cap is refused with 413."""
    client, _, home = harness
    monkeypatch.setattr("daimon_harness.outputs.OUTPUT_FILE_MAX_BYTES", 4)
    outputs = await outputs_dir(client, home)
    (outputs / "big.bin").write_bytes(b"12345")
    [file] = (await listed(client)).outputs
    assert (await client.get(f"{BASE}/{file.id}")).status_code == 413, "over the cap"


async def test_outputs_when_many_files_then_truncated(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing stops at the file cap and says so."""
    client, _, home = harness
    monkeypatch.setattr("daimon_harness.outputs.OUTPUT_FILES_MAX", 2)
    outputs = await outputs_dir(client, home)
    for index in range(4):
        (outputs / f"{index}.txt").write_text("x")
    result = await listed(client)
    assert len(result.outputs) == 2 and result.truncated, result


async def test_outputs_through_invocations_then_same_routes(harness: Harness) -> None:
    """The Foundry invocations envelope reaches list, read and delete."""
    client, _, home = harness
    outputs = await outputs_dir(client, home)
    (outputs / "a.txt").write_text("hi")

    async def invoke(method: str, path: str) -> httpx.Response:
        query = {"session_id": "session-1", "after_seq": 0}
        body = {"c3": {"method": method, "path": path, "query": query, "body": None}}
        return await client.post("/invocations", json=body)

    [file] = (await invoke("GET", BASE)).json()["outputs"]
    read = (await invoke("GET", f"{BASE}/{file['id']}")).json()
    assert base64.b64decode(read["content_b64"]) == b"hi", read
    assert (await invoke("POST", f"{BASE}/{file['id']}/delete")).json()["deleted"] is True
    assert (await invoke("POST", BASE)).status_code == 404, "unknown method must 404"


async def test_outputs_when_unauthenticated_then_rejected(harness: Harness) -> None:
    """The output routes require the same bearer token as every other route."""
    client, _, _ = harness
    response = await client.get(BASE, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401, response.text

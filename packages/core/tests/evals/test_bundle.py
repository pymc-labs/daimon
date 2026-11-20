"""The example bundle validates; a broken hash, a missing blob and an unscrubbed bundle are refused."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from daimon.core.evals.bundle import (
    ENC_MAGIC,
    DirSource,
    ZipSource,
    decrypt,
    encrypt,
    open_bundle,
    scrub_refusal,
    to_zip,
)
from daimon.core.evals.format import canonical_json, sha256_hex
from daimon.core.evals.validate import InvalidDocument, validate_bundle

EXAMPLE = Path(__file__).parent / "example_bundle"


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, target)
    return target


def test_example_bundle_when_validated_as_folder_and_zip_then_ok() -> None:
    files = {name: DirSource(EXAMPLE).read(name) for name in DirSource(EXAMPLE).names()}
    for source in (DirSource(EXAMPLE), ZipSource(to_zip(files))):
        report = validate_bundle(source)
        assert report.ok, f"the example bundle must validate: {report.problems}"
        assert len(report.suites) == 1 and len(report.runs) == 1, "suite and run are read"
    sealed = encrypt(to_zip(files), "correct horse")
    assert validate_bundle(open_bundle(sealed, passphrase="correct horse")).ok, "encrypted reads"
    with pytest.raises(ValueError, match="passphrase"):
        decrypt(sealed, "wrong")


def test_case_when_its_content_changes_then_hash_and_manifest_refuse(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    cases = root / "suites/support-basics/cases.jsonl"
    case = json.loads(cases.read_text())
    case["title"] = "Edited after hashing"
    cases.write_text(json.dumps(case) + "\n")
    problems = validate_bundle(DirSource(root)).problems
    assert any("hash or size does not match" in p for p in problems), f"file hash: {problems}"


def test_case_hash_when_manifest_is_updated_too_then_still_refused(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    path = "suites/support-basics/cases.jsonl"
    case = json.loads((root / path).read_text())
    case["title"] = "Edited after hashing"
    data = (json.dumps(case) + "\n").encode()
    (root / path).write_bytes(data)
    manifest = json.loads((root / "bundle.json").read_text())
    for entry in manifest["files"]:
        if entry["path"] == path:
            entry.update(sha256=sha256_hex(data), size=len(data))
    (root / "bundle.json").write_text(json.dumps(manifest))
    problems = validate_bundle(DirSource(root)).problems
    assert any("hash does not match its content" in p for p in problems), f"case hash: {problems}"


def test_blob_when_missing_then_refused(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    shutil.rmtree(root / "blobs")
    problems = validate_bundle(DirSource(root)).problems
    assert any("listed but missing" in p for p in problems), f"missing blob: {problems}"


def test_unscrubbed_bundle_when_not_accepted_then_refused() -> None:
    report = validate_bundle(DirSource(EXAMPLE))
    assert report.manifest is not None, "the manifest is read"
    unscrubbed = report.manifest.model_copy(
        update={"scrub": report.manifest.scrub.model_copy(update={"status": "unscrubbed"})}
    )
    assert scrub_refusal(unscrubbed, accept_unscrubbed=False), "refused without acceptance"
    assert scrub_refusal(unscrubbed, accept_unscrubbed=True) is None, "accepted explicitly"
    assert scrub_refusal(report.manifest, accept_unscrubbed=False) is None, "scrubbed passes"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, "1"), (-0.0, "0"), (1e21, "1e+21"), (1e-7, "1e-7"), (0.000001, "0.000001"),
        (123456789012345680000.0, "123456789012345680000"), (4.5, "4.5"),
        (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"),
    ],
)  # fmt: skip
def test_canonical_json_when_numbers_then_ecmascript_form(value: float, expected: str) -> None:
    assert canonical_json(value) == expected.encode(), f"RFC 8785 number form of {value!r}"


def test_canonical_json_when_keys_need_utf16_order_then_sorted_by_code_units() -> None:
    document = {"\u20ac": 1, "\U0001f600": 2, "a": [True, None, "\n"]}
    expected = '{"a":[true,null,"\\n"],"\u20ac":1,"\U0001f600":2}'
    assert canonical_json(document) == expected.encode(), "keys sort by UTF-16 code units"


def _example_files() -> dict[str, bytes]:
    return {name: DirSource(EXAMPLE).read(name) for name in DirSource(EXAMPLE).names()}


def test_encrypted_bundle_when_scrypt_costs_differ_then_refused_before_deriving() -> None:
    magic, header, sealed = encrypt(b"zip", "pw").split(b"\n", 2)
    costly = json.loads(header) | {"n": 2**20, "r": 16, "p": 4}
    forged = b"\n".join((magic, json.dumps(costly).encode(), sealed))
    assert forged.startswith(ENC_MAGIC), "still looks like an encrypted bundle"
    with pytest.raises(InvalidDocument, match="not the ones Daimon writes"):
        decrypt(forged, "pw")


def test_zip_when_names_repeat_or_entries_corrupt_then_problems_not_crashes() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive, pytest.warns(UserWarning):
        archive.writestr("bundle.json", b"{}")
        archive.writestr("bundle.json", b"{}")
    with pytest.raises(InvalidDocument, match="in the zip twice"):
        ZipSource(out.getvalue())
    data = bytearray(to_zip(_example_files()))
    at = data.index(b"bundle.json") + len("bundle.json")
    data[at : at + 64] = bytes(64)  # break the manifest's compressed bytes
    problems = validate_bundle(ZipSource(bytes(data))).problems
    assert problems, "a damaged entry is a problem, never an exception"


def test_manifest_when_paths_repeat_or_stray_then_refused_before_reading(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    manifest = json.loads((root / "bundle.json").read_text())
    run = manifest["contents"]["runs"][0]
    run["scores_path"] = run["items_path"]
    (root / "bundle.json").write_text(json.dumps(manifest))
    problems = validate_bundle(DirSource(root)).problems
    assert any("should be" in p for p in problems), f"non-canonical path: {problems}"
    manifest["contents"]["runs"].append(manifest["contents"]["runs"][0])
    (root / "bundle.json").write_text(json.dumps(manifest | {"format_version": "1.5"}))
    problems = validate_bundle(DirSource(root)).problems
    assert problems == ["bundle.json: format 1.5 is newer than this Daimon reads (1.0)"], problems


def test_folder_when_manifest_points_outside_then_refused(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    (tmp_path / "secret.txt").write_text("not part of the bundle")
    with pytest.raises(InvalidDocument, match="outside the bundle folder"):
        DirSource(root).read("../secret.txt")

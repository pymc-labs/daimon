"""Leaks found in review: each probe must come out with nothing real left, or be marked partial."""

from __future__ import annotations

import base64
import io
import json
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from daimon.core.evals.from_thread import ThreadRecord, build_cases
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.entities import KnownEntities
from daimon.core.evals.scrub.files import rebuild
from daimon.core.evals.scrub.offline import leftovers_of, load_scrubber, save_map, scrub_path
from daimon.core.evals.validate import InvalidDocument
from PIL import Image

KEY = b"k" * 32
EXAMPLE = Path(__file__).parent / "example_bundle"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
REAL = ("alice", "smith", "renée", "renee", "acme", "globex", "priya", "raman", "hunter2")
ENTITIES = KnownEntities.model_validate(
    {
        "people": [
            {
                "name": "Alice Smith",
                "emails": ["alice@acme-corp.com"],
                "ids": ["123456789012345678"],
            },
            {"name": "Renée Dubois"},
            {"name": "Priya Raman"},
        ],
        "companies": ["Acme Corp", "Globex Industries"],
    }
)


def _scrubber(entities: KnownEntities = ENTITIES, salt: str = "t1") -> Scrubber:
    return Scrubber(entities, KEY, salt=salt, ner_model=None)


def _real_in(text: str) -> list[str]:
    folded = text.casefold()
    return [r for r in REAL if r in folded]


@pytest.mark.parametrize(
    "text",
    [
        "file alice_smith_cv.docx",
        "AliceSmith and ALICESMITH",
        "line1\\nAlice Smith said hi",
        "Ren\\u00e9e Dubois",
        "Renée Dubois and Renee Dubois and Re​née Dubois",
        "https://x.example/?q=Alice%20Smith&e=alice%40acme-corp.com",
        "Acme said ok; ACME CORP; acme-corp; AcmeCorp",
        "DATABASE_URL=postgres://admin:hunter2hunter2@db.example.com:5432/prod",
        "redis://svc:hunter2hunter2@cache.example.com/0",
        "https://x.zoom.us/j/1234567?pwd=hunter2hunter2abcd",
        "aws_secret_access_key = hunter2hunter2/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "sk_live_51Hxhunter2hunter2 glpat-hunter2hunter2abcdef hf_hunter2hunter2hunter2hunter2abcdef",
        "https://docs.google.com/document/d/1hunter2hunter2hunter2hunter2hunter2abc/edit",
        "see https://acme.atlassian.net/browse/X-1",
        "call +1-415-555-0199 today",
    ],
)
def test_text_when_encoded_or_secret_then_nothing_real_left(text: str) -> None:
    out = _scrubber().text(text)
    assert _real_in(out) == [], out
    assert "415-555-0199" not in out, out


def test_common_names_when_many_fakes_exist_then_still_replaced() -> None:
    people = [f"Person{i} Surname{i}" for i in range(300)]
    common = ["John Smith", "Mary Wilson", "David Brown", "Linda Thomas"]
    scrubber = _scrubber(KnownEntities.model_validate({"people": people + common}))
    for person in people:
        scrubber.text(f"{person} wrote")
    for name in common:
        assert name not in scrubber.text(f"Yesterday {name} asked."), name


def test_value_when_keys_ids_and_nested_json_then_scrubbed_and_nothing_left() -> None:
    scrubber = _scrubber()
    doc = {
        "hash": "Alice Smith",
        "schema": "acme_corp_prod",
        "DB_PASSWORD": "hunter2hunter2",
        "privateKey": "abcdefghijklmnop",
        "AWS_SECRET_ACCESS_KEY": "abcdefghijklmnop",
        "owner": "moonshot-org",
        "repo": "moonshot-x",
        "html_url": "https://github.com/moonshot-org/moonshot-x",
        "user_id": 123456789012345678,
        "body": json.dumps({"author": "Renée Dubois", "note": "Hi,\nRenée here"}),
    }
    out = scrubber.value(doc)
    dumped = json.dumps(out, ensure_ascii=False)
    assert _real_in(dumped) == [] and "moonshot" not in dumped, dumped
    assert "abcdefghijklmnop" not in dumped and "123456789012345678" not in dumped, dumped
    assert scrubber.residue_of(out) == {}, "a second look finds nothing"


def test_overlap_when_known_first_name_inside_a_wider_name_then_whole_name_faked() -> None:
    scrubber = _scrubber(KnownEntities.model_validate({"people": ["Alice Jones", "Carol Smith"]}))
    out = scrubber.text("Yesterday Carol Smith and Alice Jones asked.")
    assert "Carol" not in out and "Smith" not in out and "Alice" not in out, out


def test_salt_when_different_then_different_fakes() -> None:
    assert _scrubber(salt="t1").text("Alice Smith") != _scrubber(salt="t2").text("Alice Smith")


def _docx() -> bytes:
    jpeg = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(jpeg, "JPEG")
    parts = {
        "[Content_Types].xml": "<Types/>",
        "word/_rels/document.xml.rels": '<Relationships><Relationship Id="r1" '
        'Target="media/image1.jpeg"/></Relationships>',
        "word/document.xml": f"<w:document {W}><w:body><w:p><w:r><w:t>Alice Smith met Priya "
        'Raman.</w:t></w:r></w:p><w:p><w:del w:author="Priya Raman"><w:r><w:delText>Note '
        "from Globex Industries</w:delText></w:r></w:del></w:p></w:body></w:document>",
        "docProps/core.xml": '<cp:coreProperties xmlns:cp="urn:cp" xmlns:dc="urn:dc">'
        "<dc:creator>Priya Raman</dc:creator></cp:coreProperties>",
        "docProps/app.xml": "<Properties><Company>Globex Industries</Company></Properties>",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in parts.items():
            z.writestr(name, text)
        z.writestr("word/media/image1.jpeg", jpeg.getvalue())
        z.writestr("word/embeddings/oleObject1.bin", b"raw Alice Smith data")
    return out.getvalue()


def test_docx_when_metadata_deletions_and_embedded_parts_then_nothing_real_and_names_kept() -> None:
    built = rebuild(_docx(), DOCX, "a.docx", _scrubber())
    assert built is not None
    with zipfile.ZipFile(io.BytesIO(built.data)) as z:
        names = z.namelist()
        dumped = b"".join(z.read(n) for n in names).decode("utf-8", "ignore")
    assert "word/media/image1.jpeg" in names, "image parts keep their name, so rels still match"
    assert _real_in(dumped) == [], dumped


def _record(files: list[dict[str, object]], result: object = None) -> ThreadRecord:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    calls = [{"tool": "crm_get", "args": {}, "result": result}] if result is not None else []
    return ThreadRecord.model_validate(
        {
            "platform": "slack", "ref": "r1", "events_recorded": True,
            "people": [
                {"id": "U1", "name": "Renée Dubois", "groups": ["globex-account-team"]},
                {"id": "UB", "name": "bot", "is_bot": True},
            ],
            "messages": [
                {"id": "1", "author": "U1", "at": at, "text": "look", "files": files, "calls": calls},
                {"id": "2", "author": "UB", "at": at + timedelta(minutes=1), "text": "done"},
            ],
        }
    )  # fmt: skip


def test_thread_when_file_cannot_be_scrubbed_then_missing_and_groups_faked() -> None:
    record = _record(
        [{"name": "notes.zip", "media_type": "application/zip", "data": "PK Renée".encode()}]
    )
    (built,) = build_cases(record, _scrubber(record.entities()))
    (attachment,) = built.case["turn"]["messages"][0]["attachments"]  # type: ignore[index]
    assert attachment["state"] == "missing" and built.blobs == {}, "raw files are never stored"
    assert _real_in(json.dumps(built.case, ensure_ascii=False)) == [], "groups are faked too"


def test_thread_when_tool_returns_binary_payload_then_dropped_and_world_incomplete() -> None:
    image = base64.b64encode(b"\x89PNG" + bytes(range(256))).decode()
    inner = json.dumps({"author": "Renée Dubois"})
    content = [{"type": "text", "text": inner}, {"type": "image", "data": image, "mimeType": "x"}]
    record = _record([], result={"content": content})
    (built,) = build_cases(record, _scrubber(record.entities()))
    dumped = json.dumps(built.case, ensure_ascii=False)
    assert image not in dumped and _real_in(dumped) == [], dumped
    flags = [f["reason"] for f in built.case["review_flags"]]  # type: ignore[attr-defined]
    assert "world_incomplete" in flags, flags


def test_bundle_when_manifest_names_people_then_scrubbed_and_map_bound_to_key(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, source)
    manifest = json.loads((source / "bundle.json").read_text())
    manifest["title"] = "Alice Smith - Globex Industries escalations"
    manifest["created_by"] = {"kind": "person", "id": "alice@acme-corp.com", "display_name": "x"}
    manifest["source"] = {"kind": "deployment", "label": "globex-prod", "instance": "https://g.io"}
    (source / "bundle.json").write_text(json.dumps(manifest))
    names, fakes = tmp_path / "names.json", tmp_path / "fakes.json"
    names.write_text(ENTITIES.model_dump_json())
    scrubber = load_scrubber(
        names, "0123456789abcdef0123", salt="t1", map_path=fakes, ner_model=None
    )
    scrub_path(source, None, scrubber)
    save_map(scrubber, fakes)
    out = (source / "bundle.json").read_text()
    assert _real_in(out) == [] and "g.io" not in out, out
    kinds = {x.kind for x in leftovers_of(source, scrubber)}
    assert kinds <= {"names_unchecked"}, "the manifest sweeps clean"
    with pytest.raises(InvalidDocument):
        load_scrubber(names, "0123456789abcdef0123", salt="t2", map_path=fakes, ner_model=None)

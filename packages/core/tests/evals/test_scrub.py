"""Scrubbing: one fake per real value everywhere, a sweep that names places not text, offline bundles."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from daimon.core.evals.bundle import DirSource
from daimon.core.evals.from_thread import (
    ThreadCall,
    ThreadFile,
    ThreadMessage,
    ThreadPerson,
    ThreadRecord,
    build_cases,
)
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.entities import KnownEntities
from daimon.core.evals.scrub.offline import leftovers_of, load_scrubber, scrub_path
from daimon.core.evals.scrub.sweep import sweep_document
from daimon.core.evals.validate import case_problems, validate_bundle

KEY = b"test-key-0123456789"
WHO, EMAIL, ID = "Maria Lindqvist", "maria@acme-corp.example", "111111111111111111"
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
EXAMPLE = Path(__file__).parent / "example_bundle"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _docx(text: str) -> bytes:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w='
        '"http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        f"{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", body)
    return out.getvalue()


def _message(n: int, author: str, text: str, **extra: object) -> ThreadMessage:
    return ThreadMessage(
        id=f"m{n}",
        author=author,
        at=T0 + timedelta(minutes=n),
        text=text,
        **extra,  # type: ignore[arg-type]
    )


def _people() -> list[ThreadPerson]:
    return [
        ThreadPerson(id=ID, name=WHO, email=EMAIL),
        ThreadPerson(id="999999999999999999", name="Helper", is_bot=True),
    ]


def test_person_when_in_text_json_file_and_name_then_one_fake_and_token_removed() -> None:
    record = ThreadRecord(
        platform="discord",
        ref="thread-1",
        people=_people(),
        events_recorded=True,
        messages=[
            _message(
                1,
                ID,
                f"I am {WHO}, mail {EMAIL}, token {TOKEN}",
                files=[
                    ThreadFile(
                        name=f"{WHO.lower().replace(' ', '-')}-notes.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        data=_docx(f"Notes by {WHO} ({EMAIL})"),
                    )
                ],
                calls=[
                    ThreadCall(tool="lookup", args={"user": ID, "who": WHO}, result={"mail": EMAIL})
                ],
            ),
            _message(2, "999999999999999999", "Done."),
        ],
    )
    scrubber = Scrubber(record.entities(), KEY, salt="t1", ner_model=None)
    (built,) = build_cases(record, scrubber)
    fake = scrubber.fakes.fake("person", WHO)
    fake_mail = scrubber.fakes.fake("email", EMAIL)
    fake_id = scrubber.fakes.fake("id", ID)
    case = built.case
    text = case["turn"]["messages"][0]["text"]  # type: ignore[index]
    exchange = json.dumps(case["world"]["exchanges"])  # type: ignore[index]
    (blob,) = built.blobs.values()
    docx_text = zipfile.ZipFile(io.BytesIO(blob.data)).read("word/document.xml").decode()
    name = case["turn"]["messages"][0]["attachments"][0]["name"]  # type: ignore[index]
    assert fake in text and fake_mail in text and TOKEN not in text, "message text"
    assert fake in exchange and fake_mail in exchange and fake_id in exchange, "JSON exchange"
    assert fake in docx_text and fake_mail in docx_text, "docx content"
    assert fake.lower().replace(" ", "-") in name, "file name"
    dumped = json.dumps(case) + docx_text
    assert not any(real in dumped for real in (WHO, EMAIL, ID, TOKEN)), "nothing real is left"
    assert case_problems(case) == [], "the scrubbed case is still a valid case"


def test_sweep_when_a_name_is_planted_then_it_is_reported_by_place_and_kind() -> None:
    document = {"id": "c1", "notes": f"Ask {WHO} about it", "title": "ok"}
    scrubber = Scrubber(KnownEntities(people=[WHO]), KEY, salt="t1", ner_model=None)
    found = sweep_document(scrubber, document, "case c1")
    assert [(x.where, x.kind, x.count) for x in found] == [("case c1", "person", 1)]
    assert WHO not in repr(found), "a leftover never carries the text"


def test_bundle_when_scrubbed_offline_then_valid_stable_and_nothing_left(tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, source)
    names = tmp_path / "names.json"
    names.write_text(json.dumps({"phrases": ["support-basics"], "companies": ["Acme Corp"]}))
    scrubber = load_scrubber(names, "0123456789abcdef0123", salt="t1", ner_model=None)
    report = scrub_path(source, tmp_path / "out", scrubber)
    out = DirSource(tmp_path / "out")
    assert validate_bundle(out).ok, "the scrubbed bundle validates"
    assert report.cases == 1 and report.leftovers == [], "no leftovers"
    assert "support-basics" not in "".join(
        out.read(n).decode("utf-8", "ignore") for n in out.names()
    )
    assert leftovers_of(tmp_path / "out", scrubber)[0].kind != "person", "nothing found again"
    again = scrub_path(tmp_path / "out", None, scrubber)
    # A real name can equal a fake, so names are replaced again; ids, emails and repos are not.
    assert set(again.replaced) <= {"company", "handle", "person", "project"}, again.replaced
    assert again.leftovers == [], "a second pass leaves nothing either"

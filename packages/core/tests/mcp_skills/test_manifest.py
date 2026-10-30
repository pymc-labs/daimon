"""Checking `skills/list` entries and their files (`daimon.core.mcp_skills.manifest`)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from daimon.core.mcp_skills.manifest import (
    MAX_FILES,
    SPEC_MAX_BYTES,
    SkillRejected,
    parse_entry,
    verify_file,
    verify_frontmatter,
)

SKILL_MD = b"---\nname: triage\ndescription: Sorts reports.\n---\nRead the report.\n"


def _file(uri: str, content: bytes) -> dict[str, Any]:
    return {
        "uri": uri,
        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
        "size": len(content),
    }


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "uri": "skill://example/triage/SKILL.md",
        "frontmatter": {"name": "triage", "description": "Sorts reports."},
        "resources": [
            _file("skill://example/triage/SKILL.md", SKILL_MD),
            _file("skill://example/triage/scripts/run.sh", b"echo hi\n"),
        ],
    }
    entry.update(overrides)
    return entry


def test_parse_entry_reads_a_static_manifest() -> None:
    """A static manifest parses into its files, keyed by path."""
    entry = parse_entry(_entry())

    assert entry is not None, "a valid entry should parse"
    assert entry.name == "triage", "the name comes from the frontmatter"
    assert [f.path for f in entry.files] == ["SKILL.md", "scripts/run.sh"], (
        "files are keyed by path, sorted"
    )


def test_parse_entry_skips_a_dynamic_skill() -> None:
    """A skill with `"dynamic"` resources cannot be verified, so it is skipped."""
    assert parse_entry(_entry(resources="dynamic")) is None, "a dynamic skill is skipped"


def test_manifest_digest_follows_content() -> None:
    """The manifest digest names the content: same listing, same digest."""
    first, again = parse_entry(_entry()), parse_entry(_entry())
    changed = parse_entry(
        _entry(
            resources=[
                _file("skill://example/triage/SKILL.md", SKILL_MD),
                _file("skill://example/triage/scripts/run.sh", b"echo bye\n"),
            ]
        )
    )

    assert first is not None and again is not None and changed is not None, (
        "all three entries are valid"
    )
    assert first.manifest_digest == again.manifest_digest, "the digest is stable"
    assert first.manifest_digest != changed.manifest_digest, "a changed file changes the digest"


def test_authority_names_a_root_skill() -> None:
    """A root-level skill is named by the URI authority."""
    entry = parse_entry(
        _entry(
            uri="skill://triage/SKILL.md", resources=[_file("skill://triage/SKILL.md", SKILL_MD)]
        )
    )

    assert entry is not None and entry.files[0].path == "SKILL.md", (
        "an authority-named skill keeps SKILL.md at its root"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"uri": "skill://example/other/SKILL.md"},
        {"uri": "skill://example/triage/README.md"},
        {"frontmatter": {"name": "claude-triage", "description": "d"}},
        {"frontmatter": {"name": "Triage", "description": "d"}},
        {"frontmatter": {"name": "triage", "description": " "}},
        {"resources": [_file("skill://example/triage/scripts/run.sh", b"x")]},
        {
            "resources": [
                _file("skill://example/triage/SKILL.md", SKILL_MD),
                _file("skill://example/other/notes.md", b"x"),
            ]
        },
        {
            "resources": [
                _file("skill://example/triage/SKILL.md", SKILL_MD),
                _file("skill://example/triage/../secrets.txt", b"x"),
            ]
        },
        {
            "resources": [
                _file("skill://example/triage/SKILL.md", SKILL_MD),
                _file("skill://example/triage/a b.md", b"x"),
            ]
        },
        {
            "resources": [
                _file("skill://example/triage/SKILL.md", SKILL_MD),
                {"uri": "skill://example/triage/x.md", "digest": "md5:00", "size": 1},
            ]
        },
        {
            "resources": [
                _file("skill://example/triage/SKILL.md", SKILL_MD),
                _file("skill://example/triage/SKILL.md", SKILL_MD),
            ]
        },
        {
            "resources": [
                _file("skill://example/triage/SKILL.md", SKILL_MD),
                {
                    "uri": "skill://example/triage/big.bin",
                    "digest": "sha256:" + "0" * 64,
                    "size": SPEC_MAX_BYTES,
                },
            ]
        },
        {
            "resources": [_file("skill://example/triage/SKILL.md", SKILL_MD)]
            + [_file(f"skill://example/triage/f{i}.md", b"x") for i in range(MAX_FILES)]
        },
    ],
    ids=[
        "dir-not-name",
        "not-skill-md",
        "reserved-name",
        "bad-name",
        "blank-description",
        "no-skill-md",
        "outside-dir",
        "dot-dot",
        "unsafe-path",
        "bad-digest",
        "listed-twice",
        "too-large",
        "too-many-files",
    ],
)
def test_parse_entry_rejects(overrides: dict[str, Any]) -> None:
    """Entries the extension or the upload forbids are rejected whole."""
    with pytest.raises(SkillRejected):
        parse_entry(_entry(**overrides))


def test_verify_file_rejects_size_and_digest_mismatch() -> None:
    """A file read must match its manifest size and SHA-256."""
    entry = parse_entry(_entry())
    assert entry is not None, "fixture entry should parse"
    skill_md = entry.files[0]

    verify_file(skill_md, SKILL_MD)
    with pytest.raises(SkillRejected, match="size"):
        verify_file(skill_md, SKILL_MD + b"\n")
    with pytest.raises(SkillRejected, match="digest"):
        verify_file(skill_md, SKILL_MD.replace(b"Read", b"Burn"))


def test_verify_frontmatter_compares_the_listing() -> None:
    """`SKILL.md` frontmatter must equal the listing's, field by field."""
    entry = parse_entry(_entry())
    assert entry is not None, "fixture entry should parse"

    verify_frontmatter(entry, SKILL_MD)
    with pytest.raises(SkillRejected, match="does not match"):
        verify_frontmatter(entry, SKILL_MD.replace(b"Sorts", b"Hides"))
    with pytest.raises(SkillRejected, match="frontmatter"):
        verify_frontmatter(entry, b"no frontmatter\n")


def test_verify_frontmatter_reads_dates_as_strings() -> None:
    """YAML dates compare equal to the ISO strings the JSON listing carries."""
    entry = parse_entry(
        _entry(frontmatter={"name": "triage", "description": "d", "since": "2026-01-02"})
    )
    assert entry is not None, "fixture entry should parse"

    verify_frontmatter(entry, b"---\nname: triage\ndescription: d\nsince: 2026-01-02\n---\n")


_BOMB = "\n".join(
    ["name: triage", "description: d", "a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    + [f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]" for i in range(1, 9)]
)


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        (_BOMB, "anchors or aliases"),
        ("name: triage\ndescription: d\nloop: &l [*l]", "anchors or aliases"),
        ("name: triage\ndescription: d\nx:\n  <<: {a: 1}", "merge key"),
        ("name: triage\ndescription: d\nx: " + "[" * 3000 + "]" * 3000, "nested too deeply"),
        ("name: triage\ndescription: " + "d" * 9000, "too large"),
    ],
    ids=["alias-bomb", "self-reference", "merge-key", "deep-nesting", "oversized"],
)
def test_frontmatter_that_could_expand_or_recurse_is_rejected(
    frontmatter: str, message: str
) -> None:
    """Anchors, aliases, merge keys, deep nesting and oversized frontmatter never parse."""
    entry = parse_entry(_entry())
    assert entry is not None, "fixture entry should parse"

    with pytest.raises(SkillRejected, match=message):
        verify_frontmatter(entry, f"---\n{frontmatter}\n---\nbody\n".encode())

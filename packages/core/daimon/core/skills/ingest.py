"""Check one pasted, uploaded or fetched skill and pack it for upload.

Pure: text or bytes in, a `SkillBundle` out. No network, disk or clock, so
every refusal below is a unit test. The shells that fetch a GitHub path or a
chat attachment (`daimon.core.skills.add`) hand their bytes here.

An uploaded archive is untrusted input, so it is unpacked in memory and
refused outright, never silently repaired, when an entry is a symlink, an
absolute path, a `..` climb, encrypted, or over the caps `build_skill_zip`
already enforces. The packed zip is byte-deterministic, so the preview's
`content_hash` names exactly what a later confirmation uploads.
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from daimon.core.defaults.loader import RESERVED_SKILL_SUBSTRINGS, parse_skill_markdown
from daimon.core.errors import DaimonError, DefaultsError
from daimon.core.skill_zip import MAX_FILES, MAX_UNCOMPRESSED_BYTES
from pydantic import BaseModel, ConfigDict

__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "SKILL_NAME_RE",
    "SkillBundle",
    "SkillIngestError",
    "SkillPreview",
    "bundle_from_files",
    "bundle_from_markdown",
    "bundle_from_upload",
]

SKILL_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
#: The provider's own bound on a skill description.
MAX_DESCRIPTION_CHARS: Final[int] = 1024

_SAFE_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._/-]+$")
_ZERO_TS: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)
# Archive tools add these beside the real files; they are never part of a skill.
_JUNK_PARTS: Final[frozenset[str]] = frozenset({"__MACOSX", ".DS_Store"})
_SCRIPT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".bash",
        ".bat",
        ".cjs",
        ".cmd",
        ".dll",
        ".dylib",
        ".exe",
        ".jar",
        ".js",
        ".mjs",
        ".php",
        ".pl",
        ".ps1",
        ".py",
        ".rb",
        ".sh",
        ".so",
        ".ts",
        ".zsh",
    }
)


class SkillIngestError(DaimonError):
    """Why a skill cannot be added, worded for the person adding it."""


class SkillPreview(BaseModel):
    """What a skill holds, shown before anything is uploaded."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    files: list[str]
    scripts: list[str]
    """Files the agent could run: a script suffix, or a `#!` first line."""
    total_bytes: int
    content_hash: str
    """sha256 of the packed zip. Confirming an upload names this value."""


@dataclass(frozen=True)
class SkillBundle:
    preview: SkillPreview
    zip_bytes: bytes


def bundle_from_markdown(text: str) -> SkillBundle:
    """A skill that is only its SKILL.md."""
    return bundle_from_files({"SKILL.md": text.encode("utf-8")})


def bundle_from_upload(data: bytes, *, filename: str) -> SkillBundle:
    """A `.md` file is the SKILL.md; a `.zip` is one skill's folder."""
    suffix = PurePosixPath(filename.lower()).suffix
    if suffix == ".md":
        try:
            return bundle_from_markdown(data.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise SkillIngestError(f"{filename} is not UTF-8 text.") from exc
    if suffix == ".zip":
        return bundle_from_files(_read_zip(data))
    raise SkillIngestError(f"{filename}: upload a SKILL.md or a .zip of one skill's folder.")


def bundle_from_files(files: dict[str, bytes]) -> SkillBundle:
    """Check a skill's files (relative posix paths) and pack them.

    A single wrapper folder is dropped, so a zip of `my-skill/` works. The
    root must hold SKILL.md and nothing below it may hold another.
    """
    files = {path: body for path, body in files.items() if not _is_junk(path)}
    for path in files:
        _require_safe_path(path)
    files = _strip_wrapper(files)
    if "SKILL.md" not in files:
        raise SkillIngestError("No SKILL.md at the top of the skill.")
    nested = sorted(path for path in files if path != "SKILL.md" and path.endswith("/SKILL.md"))
    if nested:
        raise SkillIngestError(
            f"This holds more than one skill ({', '.join(nested[:5])}); add one at a time."
        )
    if len(files) > MAX_FILES:
        raise SkillIngestError(f"A skill may hold at most {MAX_FILES} files.")
    total = sum(len(body) for body in files.values())
    if total > MAX_UNCOMPRESSED_BYTES:
        raise SkillIngestError(f"A skill may hold at most {MAX_UNCOMPRESSED_BYTES} bytes.")
    name, description = _read_frontmatter(files["SKILL.md"])
    zip_bytes = _pack(files, name=name)
    ordered = sorted(files)
    return SkillBundle(
        preview=SkillPreview(
            name=name,
            description=description,
            files=ordered,
            scripts=[path for path in ordered if _is_script(path, files[path])],
            total_bytes=total,
            content_hash=hashlib.sha256(zip_bytes).hexdigest(),
        ),
        zip_bytes=zip_bytes,
    )


def _read_frontmatter(skill_md: bytes) -> tuple[str, str]:
    try:
        text = skill_md.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillIngestError("SKILL.md is not UTF-8 text.") from exc
    try:
        spec, _body = parse_skill_markdown(text, source="SKILL.md")
    except DefaultsError as exc:
        raise SkillIngestError(str(exc)) from exc
    name = spec.name
    if not SKILL_NAME_RE.fullmatch(name):
        raise SkillIngestError(
            f"SKILL.md name {name!r} must be 1-64 lowercase letters, digits or hyphens, "
            "starting with a letter or digit."
        )
    reserved = next((word for word in RESERVED_SKILL_SUBSTRINGS if word in name), None)
    if reserved is not None:
        raise SkillIngestError(f"SKILL.md name {name!r} may not contain {reserved!r}.")
    description = spec.description.strip()
    if not description:
        raise SkillIngestError("SKILL.md needs a description saying when to use the skill.")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillIngestError(
            f"SKILL.md description is {len(description)} characters; the limit is "
            f"{MAX_DESCRIPTION_CHARS}."
        )
    return name, description


def _read_zip(data: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillIngestError("That file is not a readable zip.") from exc
    files: dict[str, bytes] = {}
    budget = MAX_UNCOMPRESSED_BYTES
    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if len(entries) > MAX_FILES * 2:
            raise SkillIngestError(f"A skill may hold at most {MAX_FILES} files.")
        for info in entries:
            path = info.filename
            if stat.S_ISLNK(info.external_attr >> 16):
                raise SkillIngestError(f"{path} is a symlink; skills may not hold links.")
            if info.flag_bits & 0x1:
                raise SkillIngestError(f"{path} is encrypted.")
            if _is_junk(path):
                continue
            _require_safe_path(path)
            if path in files:
                raise SkillIngestError(f"{path} appears twice in the zip.")
            # Read at most one byte past the budget: a header can understate
            # the size, and a bomb must stop here, not after it has expanded.
            with archive.open(info) as member:
                body = member.read(budget + 1)
            if len(body) > budget:
                raise SkillIngestError(f"A skill may hold at most {MAX_UNCOMPRESSED_BYTES} bytes.")
            budget -= len(body)
            files[path] = body
    return files


def _require_safe_path(path: str) -> None:
    # The character set already refuses drive letters and backslashes.
    if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
        raise SkillIngestError(f"{path!r} is not a plain relative path inside the skill.")
    if not _SAFE_PATH_RE.fullmatch(path):
        raise SkillIngestError(
            f"{path!r}: file names may use only letters, digits, '.', '_', '-' and '/'."
        )


def _is_junk(path: str) -> bool:
    return any(part in _JUNK_PARTS or part.startswith("._") for part in path.split("/"))


def _strip_wrapper(files: dict[str, bytes]) -> dict[str, bytes]:
    if "SKILL.md" in files or not files:
        return files
    tops = {path.split("/", 1)[0] for path in files}
    if len(tops) != 1 or any("/" not in path for path in files):
        return files
    prefix = f"{next(iter(tops))}/"
    return {path.removeprefix(prefix): body for path, body in files.items()}


def _is_script(path: str, body: bytes) -> bool:
    return PurePosixPath(path).suffix.lower() in _SCRIPT_SUFFIXES or body.startswith(b"#!")


def _pack(files: dict[str, bytes], *, name: str) -> bytes:
    """Zip under `name/` (the provider requires it to match SKILL.md), byte-for-byte stable."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(filename=f"{name}/{path}", date_time=_ZERO_TS)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0
            archive.writestr(info, files[path])
    return buffer.getvalue()

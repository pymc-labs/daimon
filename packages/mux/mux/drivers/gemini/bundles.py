"""Pinned host catalogues compiled into documented inline environment sources."""

import gzip
import hashlib
import io
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import JsonValue

from mux.contracts.ids import Scope, SkillRef
from mux.contracts.resources import SkillUpload, WorkspaceSource
from mux.drivers.gemini.skill_deployment import validate_deployment
from mux.drivers.gemini.storage import Records, SkillRecord, UploadedBundle, owner
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles.gemini import INLINE_REUSE
from mux.state.operations import request_digest

SKILL_SOURCE = "gemini.inline"
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_INLINE_FILE_BYTES = 1024 * 1024
MAX_DEPLOYED_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_BUNDLE_BYTES = 28 * 1024 * 1024
SKILLS_ROOT = ".agents/skills"
#: Where an uploaded skill's files land: the inline sources' root.
UPLOAD_ROOT = SKILLS_ROOT


def relative_path(path: str) -> str:
    parsed = PurePosixPath(path)
    if not path or "\\" in path or "\x00" in path or parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("expected a relative resource path without traversal")
    canonical = parsed.as_posix()
    if canonical == ".":
        raise ValueError("resource path must name a file")
    return canonical


def target_path(path: str) -> str:
    if path.startswith("/"):
        return "/" + relative_path(path[1:])
    return relative_path(path)


def text_bytes(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise UnsupportedCapability(("binary_inline_source",), INLINE_REUSE.profile_id) from None


def fits_inline(bundle: SkillUpload) -> bool:
    """Whether a bundle mounts as inline sources: UTF-8 text within the inline limits."""
    if sum(len(file.content) for file in bundle.files) > MAX_BUNDLE_BYTES:
        return False
    for file in bundle.files:
        try:
            file.content.decode("utf-8")
        except UnicodeDecodeError:
            return False
        if len(file.content) > MAX_INLINE_FILE_BYTES:
            return False
    return True


def fits_upload(bundle: SkillUpload) -> bool:
    """Whether a bundle past the inline limits uploads: only its SKILL.md mounts inline."""
    manifest = next(file.content for file in bundle.files if file.path == "SKILL.md")
    return (
        sum(len(file.content) for file in bundle.files) <= MAX_UPLOAD_BUNDLE_BYTES
        and len(manifest) <= MAX_INLINE_FILE_BYTES
    )


def validate_bundle(bundle: SkillUpload, *, external: bool = False, upload: bool = False) -> str:
    if not bundle.files or len(bundle.files) > 256:
        raise ValueError("skill bundle needs between one and 256 files")
    paths = [relative_path(file.path) for file in bundle.files]
    if any(path != file.path for path, file in zip(paths, bundle.files, strict=True)):
        raise ValueError("skill paths must be canonical")
    if len(set(paths)) != len(paths) or "SKILL.md" not in paths:
        raise ValueError("skill bundle needs unique paths and a root SKILL.md")
    if any(str(parent) in paths for path in paths for parent in PurePosixPath(path).parents):
        raise ValueError("skill bundle file paths overlap")
    try:
        next(file.content for file in bundle.files if file.path == "SKILL.md").decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("root SKILL.md must be UTF-8 text") from None
    if sum(len(file.content) for file in bundle.files) > MAX_DEPLOYED_BUNDLE_BYTES:
        raise ValueError("skill bundle exceeds the deployment size limit")
    if not external and not fits_inline(bundle) and not (upload and fits_upload(bundle)):
        raise UnsupportedCapability(("skill_bundle_gcs_deployment",), INLINE_REUSE.profile_id)
    canonical = bundle.model_copy(
        update={"files": tuple(sorted(bundle.files, key=lambda f: f.path))}
    )
    return request_digest(canonical.model_dump(mode="json"))


def upload_manifest(bundle: SkillUpload) -> UploadedBundle:
    """What a record keeps of an uploaded version: the SKILL.md, the rest by digest."""
    files = sorted(
        (file.path, len(file.content), hashlib.sha256(file.content).hexdigest())
        for file in bundle.files
        if file.path != "SKILL.md"
    )
    manifest = next(file.content for file in bundle.files if file.path == "SKILL.md")
    return UploadedBundle(manifest.decode("utf-8"), tuple(files))


def skill_archive(uploaded: UploadedBundle, bundle: SkillUpload) -> bytes:
    """A tar.gz of an uploaded version's files past SKILL.md, each checked against its manifest.

    Regular files only, at validated relative paths, so it can't write outside its directory.
    """
    contents = {file.path: file.content for file in bundle.files}
    expected = {path for path, _, _ in uploaded.files}
    if (
        set(contents) - {"SKILL.md"} != expected
        or contents.get("SKILL.md") != uploaded.manifest.encode("utf-8")
        or any(
            len(contents[path]) != size or hashlib.sha256(contents[path]).hexdigest() != sha256
            for path, size, sha256 in uploaded.files
        )
    ):
        raise ProviderError("conflict", retryable=False, native_code="skill_bytes_mismatch")
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path, size, _ in uploaded.files:
            info = tarfile.TarInfo(relative_path(path))
            info.size, info.mode = size, 0o644
            archive.addfile(info, io.BytesIO(contents[path]))
    return buffer.getvalue()


def uploaded_pins(
    records: Records, scope: Scope, account: str, pins: tuple[SkillRef, ...]
) -> tuple[SkillRef, ...]:
    """The pins whose versions upload, in order, once each."""
    found: dict[str, SkillRef] = {}
    for pin in pins:
        record = owned_skill(records, scope, account, pin.id)
        if pin.version is not None and pin.version in record.uploads:
            found.setdefault(pin.id, pin)
    return tuple(found.values())


def owned_skill(records: Records, scope: Scope, account: str, skill_id: str) -> SkillRecord:
    record = records.skills.get(skill_id)
    if record is None or record.owner != owner(scope) or record.account_scope_id != account:
        raise ScopeViolation(skill_id, "no owned inline skill catalogue record")
    return record


def skill_sources(
    records: Records, scope: Scope, account: str, pins: tuple[SkillRef, ...] | None
) -> list[JsonValue]:
    sources: list[JsonValue] = []
    ids: set[str] = set()
    for pin in pins or ():
        record = owned_skill(records, scope, account, pin.id)
        if pin.id in ids or pin.version is None or pin.source not in (None, SKILL_SOURCE):
            raise UnsupportedCapability(("pinned_inline_skill",), INLINE_REUSE.profile_id)
        ids.add(pin.id)
        bundle = record.bundles.get(pin.version)
        uploaded = record.uploads.get(pin.version)
        if (bundle is None and uploaded is None) or (
            pin.digest is not None and pin.digest != pin.version
        ):
            raise ProviderError("conflict", retryable=False, native_code="skill_pin_mismatch")
        if bundle is None:
            # Only the SKILL.md mounts, so the agent lists the skill; the rest is uploaded.
            assert uploaded is not None, "an uploaded version without a bundle"
            sources.append(
                {
                    "type": "inline",
                    "target": f"{SKILLS_ROOT}/{pin.id}/SKILL.md",
                    "content": uploaded.manifest,
                }
            )
            continue
        deployment = record.deployments.get(pin.version)
        if deployment is not None:
            validate_deployment(deployment, scope, account, pin.version, bundle)
            sources.append(
                {"type": "gcs", "source": deployment.source, "target": f"{SKILLS_ROOT}/{pin.id}"}
            )
            continue
        for file in bundle.files:
            sources.append(
                {
                    "type": "inline",
                    "target": f"{SKILLS_ROOT}/{pin.id}/{file.path}",
                    "content": text_bytes(file.content),
                }
            )
    return sources


def inline_files(
    records: Records, scope: Scope, account: str, sources: tuple[WorkspaceSource, ...] | None
) -> dict[str, str]:
    result: dict[str, str] = {}
    for source in sources or ():
        if source.kind != "file":
            continue
        ref = source.artifact
        if ref is None or source.ref or source.repository_url or source.credential_ref:
            raise UnsupportedCapability(("workspace_source",), INLINE_REUSE.profile_id)
        if (ref.provider, ref.kind, ref.account_scope_id, ref.tenant_id, ref.account_id) != (
            "gemini",
            "artifact",
            account,
            scope.tenant_id,
            scope.account_id,
        ):
            raise ScopeViolation(ref.id, "inline file is outside the authorized scope")
        record = records.artifacts.get(ref.id)
        if record is None or record.artifact.ref != ref:
            raise ScopeViolation(ref.id, "no owned inline file record")
        if record.body is None:
            raise UnsupportedCapability(("snapshot_artifact_mount",), INLINE_REUSE.profile_id)
        target_path(source.target_path)
        result[ref.id] = text_bytes(record.body)
    return result


def validate_targets(sources: list[JsonValue]) -> None:
    """Reject ambiguous mounts, including workspace-relative aliases."""
    targets: list[str] = []
    inline_bytes = 0
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("expected a workspace source object")
        if source.get("type") == "inline":
            content = source.get("content")
            if not isinstance(content, str):
                raise ValueError("expected inline source text")
            size = len(content.encode("utf-8"))
            inline_bytes += size
            if size > MAX_INLINE_FILE_BYTES or inline_bytes > MAX_BUNDLE_BYTES:
                raise UnsupportedCapability(
                    ("skill_bundle_gcs_deployment",), INLINE_REUSE.profile_id
                )
        path = source.get("target")
        if not isinstance(path, str):
            raise ValueError("expected a workspace source target")
        canonical = target_path(path)
        target = canonical if canonical.startswith("/") else f"/workspace/{canonical}"
        if any(
            target == prior or target.startswith(prior + "/") or prior.startswith(target + "/")
            for prior in targets
        ):
            raise ValueError("workspace source targets overlap")
        targets.append(target)


@dataclass(frozen=True)
class MountedFiles:
    """What Daimon placed in a session's workspace; none of it is the agent's output.

    An installed skill's own files stay out even when edited; new files beside them
    are the agent's. An inline input stays out while its bytes are the mounted ones.
    Anything under a mounted directory whose files are unknown (a clone) stays out.
    """

    paths: frozenset[str]
    files: dict[str, str]
    directories: tuple[str, ...]

    def covers(self, path: str, sha256: str) -> bool:
        return (
            path in self.paths
            or self.files.get(path) == sha256
            or any(path == d or path.startswith(d + "/") for d in self.directories)
        )


def mounted_files(environment: JsonValue, skills: Mapping[str, SkillRecord]) -> MountedFiles:
    """Read the mounts from a session's saved request, so older sessions are covered too."""
    paths: set[str] = set()
    files: dict[str, str] = {}
    directories: list[str] = []
    sources = environment.get("sources") if isinstance(environment, dict) else None
    for source in sources if isinstance(sources, list) else ():
        target = source.get("target") if isinstance(source, dict) else None
        if not isinstance(source, dict) or not isinstance(target, str):
            continue
        path = target_path(target)
        if path.startswith("/"):
            # The snapshot holds /workspace only; other absolute targets never appear in it.
            if not path.startswith("/workspace/"):
                continue
            path = path.removeprefix("/workspace/")
        parts: list[str] = list(PurePosixPath(path).parts)
        content = source.get("content")
        skill = path.startswith(SKILLS_ROOT + "/") and len(parts) > 2
        if skill and source.get("type") == "inline":
            paths.add(path)
            if len(parts) == 4 and parts[3] == "SKILL.md" and isinstance(content, str):
                directory = "/".join(parts[:3])
                paths.update(
                    f"{directory}/{file}" for file in uploaded_files(skills, parts[2], content)
                )
        elif skill and (manifest := deployed_files(skills, parts[2], source.get("source"))):
            paths.update(target_path(f"{path}/{file}") for file in manifest)
        elif source.get("type") == "inline" and isinstance(content, str):
            files[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        else:
            directories.append(path)
    return MountedFiles(frozenset(paths), files, tuple(directories))


def uploaded_files(skills: Mapping[str, SkillRecord], skill_id: str, manifest: str) -> list[str]:
    """The paths uploaded beside a mounted SKILL.md, or none when it names no upload."""
    record = skills.get(skill_id)
    for uploaded in record.uploads.values() if record else ():
        if uploaded.manifest == manifest:
            return [path for path, _, _ in uploaded.files]
    return []


def deployed_files(skills: Mapping[str, SkillRecord], skill_id: str, uri: JsonValue) -> list[str]:
    """The file paths of the GCS bundle a skill mount names, or none when unknown."""
    record = skills.get(skill_id)
    for deployment in record.deployments.values() if record else ():
        if deployment.source == uri:
            return [path for path, _ in deployment.files]
    return []

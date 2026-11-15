"""Pinned host catalogues compiled into documented inline environment sources."""

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import JsonValue

from mux.contracts.ids import Scope, SkillRef
from mux.contracts.resources import SkillUpload, WorkspaceSource
from mux.drivers.gemini.skill_deployment import validate_deployment
from mux.drivers.gemini.storage import Records, SkillRecord, owner
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles.gemini import INLINE_REUSE
from mux.state.operations import request_digest

SKILL_SOURCE = "gemini.inline"
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_INLINE_FILE_BYTES = 1024 * 1024
MAX_DEPLOYED_BUNDLE_BYTES = 64 * 1024 * 1024
SKILLS_ROOT = ".agents/skills"


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


def validate_bundle(bundle: SkillUpload, *, external: bool = False) -> str:
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
    if not external:
        if sum(len(file.content) for file in bundle.files) > MAX_BUNDLE_BYTES or any(
            len(file.content) > MAX_INLINE_FILE_BYTES for file in bundle.files
        ):
            raise UnsupportedCapability(("skill_bundle_gcs_deployment",), INLINE_REUSE.profile_id)
        for file in bundle.files:
            try:
                text_bytes(file.content)
            except UnsupportedCapability:
                raise UnsupportedCapability(
                    ("skill_bundle_gcs_deployment",), INLINE_REUSE.profile_id
                ) from None
    canonical = bundle.model_copy(
        update={"files": tuple(sorted(bundle.files, key=lambda f: f.path))}
    )
    return request_digest(canonical.model_dump(mode="json"))


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
        if bundle is None or (pin.digest is not None and pin.digest != pin.version):
            raise ProviderError("conflict", retryable=False, native_code="skill_pin_mismatch")
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

    Everything under a mounted directory (an installed skill, a repository clone)
    stays out even when edited. An inline file stays out while its bytes are the
    mounted ones; once the agent changes it, it is the agent's work.
    """

    directories: tuple[str, ...]
    files: dict[str, str]

    def covers(self, path: str, sha256: str) -> bool:
        return self.files.get(path) == sha256 or any(
            path == directory or path.startswith(directory + "/") for directory in self.directories
        )


def mounted_files(environment: JsonValue) -> MountedFiles:
    """Read the mounts from a session's saved request, so older sessions are covered too."""
    directories: list[str] = []
    files: dict[str, str] = {}
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
        parts = PurePosixPath(path).parts
        content = source.get("content")
        if path.startswith(SKILLS_ROOT + "/") and len(parts) > 2:
            directories.append("/".join(parts[:3]))
        elif source.get("type") == "inline" and isinstance(content, str):
            files[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        else:
            directories.append(path)
    return MountedFiles(tuple(directories), files)

"""Inline host catalogues and binary workspace snapshots; no fictional native resources."""

import gzip
import hashlib
import io
import tarfile
import zlib
from collections.abc import AsyncIterator
from uuid import uuid4

from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope, SkillRef
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import Artifact, Skill, SkillUpload, SkillVersion
from mux.drivers.gemini.bundles import SKILL_SOURCE, owned_skill, relative_path, validate_bundle
from mux.drivers.gemini.core import Base, now, page_values, refuse
from mux.drivers.gemini.storage import ArtifactRecord, Records, SkillRecord, owner
from mux.drivers.gemini.transport import Object, close_iterator
from mux.errors import ContinuityLost, OperationConflict, ProviderError, ScopeViolation
from mux.state.operations import request_digest

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def parse_snapshot(body: bytes) -> dict[str, bytes]:
    """Never extract to disk, follow links, or accept incomplete/oversized archives."""
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise ProviderError("upstream", retryable=False, native_code="snapshot_too_large")
    try:
        if body.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                body = compressed.read(MAX_SNAPSHOT_BYTES + 1)
            if len(body) > MAX_SNAPSHOT_BYTES:
                raise ProviderError("upstream", retryable=False, native_code="snapshot_too_large")
        files: dict[str, bytes] = {}
        total, members = 0, 0
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as archive:
            for member in archive:
                members += 1
                if members > 2048:
                    raise ValueError("too many archive members")
                if member.isdir():
                    if member.size != 0:
                        raise ValueError("directory member carries data")
                    if member.name not in (".", "./"):
                        relative_path(member.name)
                    continue
                path = relative_path(member.name)
                total += member.size
                if (
                    not member.isfile()
                    or path in files
                    or member.size > MAX_ARTIFACT_BYTES
                    or total > MAX_SNAPSHOT_BYTES
                ):
                    raise ValueError("unsafe archive member")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("missing archive member body")
                with source:
                    data = source.read(MAX_ARTIFACT_BYTES + 1)
                if len(data) != member.size:
                    raise ValueError("incomplete archive member")
                files[path] = data
        return files
    except (tarfile.TarError, OSError, EOFError, ValueError, zlib.error):
        raise ProviderError("upstream", retryable=False, native_code="invalid_snapshot") from None


def delete_replay(records: Records, scope: Scope, key: str, digest: str) -> DeletionReceipt | None:
    operation = (*owner(scope), key)
    prior = records.deletions.get(operation)
    if prior is not None:
        if prior[0] != scope.principal_id:
            raise ScopeViolation(key, "operation belongs to another principal")
        if prior[1] != digest:
            raise OperationConflict(key)
        return prior[2]
    created = records.creations.get(operation)
    if created is not None:
        if created[0] != scope.principal_id:
            raise ScopeViolation(key, "operation belongs to another principal")
        raise OperationConflict(key)
    return None


class GeminiSkills(Base):
    async def create(self, scope: Scope, bundle: SkillUpload, *, key: str) -> Skill:
        version = validate_bundle(bundle)
        digest = request_digest({"kind": "skill", "account": self._account, "bundle": version})
        async with self._storage.transaction() as records:
            prior = self._creation(records, scope, key, digest)
            if prior is not None:
                record = owned_skill(records, scope, self._account, prior)
                return record.skill.model_copy(
                    update={
                        "latest_version": record.versions[version].ref,
                        "display_title": bundle.display_title,
                        "updated_at": None,
                    }
                )
            id_ = str(uuid4())
            ref = SkillRef(id=id_, version=version, digest=version, source=SKILL_SOURCE)
            skill = Skill(
                id=id_,
                display_title=bundle.display_title,
                latest_version=ref,
                source=SKILL_SOURCE,
                created_at=now(),
                native={"deployment": "inline_on_interaction", "provider_upload": False},
            )
            record = SkillRecord(owner(scope), self._account, skill)
            record.bundles[version] = bundle
            record.versions[version] = SkillVersion(
                ref=ref, version=version, created_at=skill.created_at
            )
            records.skills[id_] = record
            records.creations[(*owner(scope), key)] = scope.principal_id, digest, id_
            return skill

    async def publish_version(
        self, scope: Scope, skill_id: str, bundle: SkillUpload, *, key: str
    ) -> SkillVersion:
        version = validate_bundle(bundle)
        digest = request_digest(
            {
                "kind": "skill_version",
                "account": self._account,
                "skill": skill_id,
                "bundle": version,
            }
        )
        async with self._storage.transaction() as records:
            record = owned_skill(records, scope, self._account, skill_id)
            prior = self._creation(records, scope, key, digest)
            if prior is not None:
                return record.versions[prior]
            ref = SkillRef(id=skill_id, version=version, digest=version, source=SKILL_SOURCE)
            item = record.versions.get(version) or SkillVersion(
                ref=ref, version=version, created_at=now()
            )
            record.bundles[version], record.versions[version] = bundle, item
            record.skill = record.skill.model_copy(
                update={
                    "latest_version": ref,
                    "display_title": bundle.display_title,
                    "updated_at": now(),
                }
            )
            records.creations[(*owner(scope), key)] = scope.principal_id, digest, version
            return item

    async def retrieve(self, scope: Scope, skill_id: str) -> Skill:
        async with self._storage.transaction() as records:
            return owned_skill(records, scope, self._account, skill_id).skill

    async def list(self, scope: Scope, *, page: PageRequest) -> Page[Skill]:
        async with self._storage.transaction() as records:
            return page_values(
                [
                    r.skill
                    for r in records.skills.values()
                    if r.owner == owner(scope) and r.account_scope_id == self._account
                ],
                page,
            )

    async def delete(self, scope: Scope, skill_id: str, *, key: str) -> DeletionReceipt:
        digest = request_digest(
            {"kind": "skill_delete", "account": self._account, "skill": skill_id}
        )
        async with self._storage.transaction() as records:
            prior = delete_replay(records, scope, key, digest)
            if prior is not None:
                return prior
            owned_skill(records, scope, self._account, skill_id)
            if any(
                a.ref.tenant_id == scope.tenant_id
                and a.ref.account_id == scope.account_id
                and a.ref.account_scope_id == self._account
                and any(pin.id == skill_id for pin in a.spec.skills or ())
                for a in records.agents.values()
            ):
                raise ProviderError("conflict", retryable=False, native_code="skill_in_use")
            del records.skills[skill_id]
            receipt = DeletionReceipt(
                operation_id=key, deleted=(self._ref(scope, skill_id, "skill"),)
            )
            records.deletions[(*owner(scope), key)] = scope.principal_id, digest, receipt
            return receipt


class GeminiArtifacts(Base):
    def _artifact(self, records: Records, scope: Scope, ref: ResourceRef) -> ArtifactRecord:
        self._check(scope, ref, "artifact")
        record = records.artifacts.get(ref.id)
        if record is None or record.artifact.ref != ref:
            raise ScopeViolation(ref.id, "no owned artifact record")
        return record

    async def upload(
        self, scope: Scope, body: AsyncIterator[bytes], *, filename: str, media_type: str, key: str
    ) -> Artifact:
        if not filename or not media_type:
            raise ValueError("artifact filename and media type must be nonempty")
        data = bytearray()
        try:
            async for chunk in body:
                if len(data) + len(chunk) > MAX_ARTIFACT_BYTES:
                    raise ProviderError(
                        "invalid_request", retryable=False, native_code="artifact_too_large"
                    )
                data.extend(chunk)
        finally:
            await close_iterator(body)
        contents = bytes(data)
        sha = hashlib.sha256(contents).hexdigest()
        digest = request_digest(
            {
                "kind": "artifact_upload",
                "account": self._account,
                "filename": filename,
                "media_type": media_type,
                "sha256": sha,
            }
        )
        async with self._storage.transaction() as records:
            prior = self._creation(records, scope, key, digest)
            if prior is not None:
                return self._artifact(records, scope, self._ref(scope, prior, "artifact")).artifact
            id_ = str(uuid4())
            artifact = Artifact(
                ref=self._ref(scope, id_, "artifact"),
                filename=filename,
                media_type=media_type,
                size_bytes=len(contents),
                created_at=now(),
                native={"storage": "host_inline"},
            )
            records.artifacts[id_] = ArtifactRecord(artifact, sha, contents)
            records.creations[(*owner(scope), key)] = scope.principal_id, digest, id_
            return artifact

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Artifact:
        async with self._storage.transaction() as records:
            return self._artifact(records, scope, ref).artifact

    async def _snapshot(
        self, scope: Scope, session: ResourceRef
    ) -> tuple[str | None, str | None, dict[str, bytes]]:
        async with self._storage.transaction() as records:
            record = self._record(records, scope, session)
            env, root, binding = record.environment_id, record.root, record.session.binding.id
            if env is None:
                if record.current is None:
                    return None, None, {}
                raise ContinuityLost(binding, ("Gemini did not identify the existing workspace.",))
        try:
            data = await self._transport.download_snapshot(env)
        except ProviderError as exc:
            if exc.category == "not_found":
                raise ContinuityLost(
                    binding, ("Gemini workspace snapshot is unavailable.",)
                ) from None
            raise
        files = parse_snapshot(data)
        async with self._storage.transaction() as records:
            current = self._record(records, scope, session)
            if current.environment_id != env:
                raise ContinuityLost(binding, ("Gemini replaced the snapshot workspace.",))
            if current.root != root:
                raise ProviderError(
                    "conflict", retryable=False, native_code="snapshot_turn_changed"
                )
        return env, root, files

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest, turn_id: str | None = None
    ) -> Page[Artifact]:
        order = page.order or "asc"
        if page.cursor is None:
            env, root, files = await self._snapshot(scope, session)
            if turn_id is not None and turn_id != root:
                refuse("historical_workspace_snapshot")
            ids: list[str] = []
            async with self._storage.transaction() as records:
                current = self._record(records, scope, session)
                if (current.environment_id, current.root) != (env, root):
                    raise ProviderError(
                        "conflict", retryable=False, native_code="snapshot_turn_changed"
                    )
                for path, data in sorted(files.items(), reverse=order == "desc"):
                    sha = hashlib.sha256(data).hexdigest()
                    id_ = request_digest(
                        {
                            "session": session.id,
                            "environment": env,
                            "root": root,
                            "path": path,
                            "sha": sha,
                        }
                    )
                    artifact = Artifact(
                        ref=self._ref(scope, id_, "artifact"),
                        filename=path,
                        media_type="application/octet-stream",
                        size_bytes=len(data),
                        session=session,
                        turn_id=root,
                        created_at=now(),
                        native={
                            "storage": "environment_snapshot",
                            "environment_id": env,
                            "sha256": sha,
                        },
                    )
                    records.artifacts.setdefault(id_, ArtifactRecord(artifact, sha))
                    ids.append(id_)
                query: Object = {
                    "session": session.id,
                    "turn_id": turn_id,
                    "ids": [id_ for id_ in ids],
                    "order": order,
                }
                snapshot = request_digest(query)
                records.artifact_pages[snapshot] = session.id, turn_id, order, tuple(ids)
                start = 0
        else:
            try:
                snapshot, offset = page.cursor.split(":", 1)
                start = int(offset)
            except ValueError:
                raise ValueError("invalid snapshot cursor") from None
        async with self._storage.transaction() as records:
            self._record(records, scope, session)
            listing = records.artifact_pages.get(snapshot)
            if listing is None or listing[:3] != (session.id, turn_id, order):
                raise ScopeViolation(session.id, "snapshot cursor does not belong to this query")
            ids = list(listing[3])
            if start < 0 or start > len(ids):
                raise ValueError("invalid snapshot cursor offset")
            end = min(start + (page.limit or 100), len(ids))
            return Page(
                data=tuple(records.artifacts[id_].artifact for id_ in ids[start:end]),
                has_more=end < len(ids),
                next_cursor=f"{snapshot}:{end}" if end < len(ids) else None,
            )

    async def download(self, scope: Scope, ref: ResourceRef) -> AsyncIterator[bytes]:
        async with self._storage.transaction() as records:
            record = self._artifact(records, scope, ref)
            artifact, body, digest = record.artifact, record.body, record.digest
            binding = (
                self._record(records, scope, artifact.session).session.binding.id
                if body is None and artifact.session is not None
                else ""
            )
        if body is None:
            if artifact.session is None:
                raise ProviderError(
                    "upstream", retryable=False, native_code="missing_artifact_session"
                )
            env, _, files = await self._snapshot(scope, artifact.session)
            if artifact.native is None or not isinstance(artifact.native, dict):
                raise ProviderError(
                    "upstream", retryable=False, native_code="invalid_artifact_metadata"
                )
            if env != artifact.native.get("environment_id"):
                raise ContinuityLost(binding, ("Snapshot workspace identity changed.",))
            body = files.get(artifact.filename)
            if body is None:
                raise ProviderError(
                    "not_found", retryable=False, native_code="artifact_unavailable"
                )
            if hashlib.sha256(body).hexdigest() != digest:
                raise ProviderError("conflict", retryable=False, native_code="artifact_changed")
        for start in range(0, len(body), 65536):
            yield body[start : start + 65536]

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._check(scope, ref, "artifact")
        digest = request_digest({"kind": "artifact_delete", "ref": ref.model_dump(mode="json")})
        async with self._storage.transaction() as records:
            prior = delete_replay(records, scope, key, digest)
            if prior is not None:
                return prior
            record = self._artifact(records, scope, ref)
            if record.body is None:
                refuse("workspace_artifact_delete")
            if any(
                source.artifact == ref
                for env in records.environments.values()
                for source in env.spec.sources or ()
            ):
                raise ProviderError("conflict", retryable=False, native_code="artifact_in_use")
            del records.artifacts[ref.id]
            receipt = DeletionReceipt(operation_id=key, deleted=(ref,))
            records.deletions[(*owner(scope), key)] = scope.principal_id, digest, receipt
            return receipt

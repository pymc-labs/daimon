"""Host resource refusal and upload recovery without any native SDK model."""

from dataclasses import dataclass

import pytest
from daimon.core.skills.ingest import SkillBundle, SkillIngestError, bundle_from_markdown
from daimon.core.skills.service import StoredSkillUpload, UploadedSkill, add_skill


@dataclass
class Host:
    stored: StoredSkillUpload | None = None
    uploads: int = 0
    attaches: int = 0
    deny: bool = False

    def attached_ids(self, agent: int) -> frozenset[str]:
        return frozenset()

    def skill_count(self, agent: int) -> int:
        return 0

    async def load_upload(self, name: str) -> StoredSkillUpload | None:
        return self.stored

    async def find_own_skill(self, name: str) -> str | None:
        return None

    async def refuse_mount_clash(self, agent: int, name: str, own_id: str | None) -> None:
        pass

    async def retrieve_agent(self) -> int:
        return 42

    async def recheck(self, agent: int) -> None:
        if self.deny:
            raise SkillIngestError("The host policy changed.")

    async def refuse_shared(self, agent: int, name: str, skill_id: str) -> None:
        pass

    async def create_skill(self, bundle: SkillBundle) -> UploadedSkill:
        self.uploads += 1
        return UploadedSkill("opaque-upload", "revision-A")

    async def publish_skill(self, skill_id: str, bundle: SkillBundle) -> UploadedSkill:
        raise AssertionError("An unchanged retry must not publish another version")

    async def save_upload(self, bundle: SkillBundle, uploaded: UploadedSkill) -> None:
        self.stored = StoredSkillUpload(
            uploaded.id, uploaded.version, bundle.preview.content_hash, "upload", ""
        )

    async def attach(self, skill_id: str) -> bool:
        # A host must recheck again during attach, including a cached upload.
        await self.recheck(await self.retrieve_agent())
        self.attaches += 1
        if self.attaches == 1:
            raise RuntimeError("Upload committed; attachment failed")
        return True


def bundle() -> SkillBundle:
    return bundle_from_markdown("---\nname: notes\ndescription: Write notes.\n---\nWrite them.\n")


async def test_a_failed_attachment_reuses_the_recorded_upload_on_retry() -> None:
    host = Host()
    with pytest.raises(RuntimeError, match="attachment failed"):
        await add_skill(host, agent=42, agent_name="neutral", bundle=bundle())
    assert host.stored is not None
    result = await add_skill(host, agent=42, agent_name="neutral", bundle=bundle())
    assert (result.action, result.skill_id, result.version) == (
        "unchanged",
        "opaque-upload",
        "revision-A",
    )
    assert result.newly_attached
    assert host.uploads == 1 and host.attaches == 2


async def test_fresh_policy_refusal_precedes_upload_and_ledger_write() -> None:
    host = Host(deny=True)
    with pytest.raises(SkillIngestError, match="policy changed"):
        await add_skill(host, agent=42, agent_name="neutral", bundle=bundle())
    assert host.uploads == host.attaches == 0
    assert host.stored is None

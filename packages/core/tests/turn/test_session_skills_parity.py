"""Every profile that installs a skill bundle attaches an agent's custom and channel skills."""

from __future__ import annotations

from daimon.core.turn.session_skills import attaches_session_skills
from mux.profiles import PROFILES

#: Profiles that take a bundle but carry no template's skills per session, and why.
OTHER_WAY = {
    "anthropic.managed_agents": "the source provider: its skills ride on the agent itself",
    "selfhosted.loop": "its skills support is unknown until the server is probed",
}


def test_every_profile_with_a_skills_bundle_attaches_custom_and_channel_skills() -> None:
    for profile_id, profile in PROFILES.items():
        if profile.support_for("skills_bundle") == "unsupported":
            assert not attaches_session_skills(profile), profile_id
            continue
        assert attaches_session_skills(profile) or profile_id in OTHER_WAY, profile_id
    for profile_id in OTHER_WAY:
        assert not attaches_session_skills(PROFILES[profile_id]), f"{profile_id} closed its gap"


def test_gemini_keeps_skills_within_its_shared_inline_budget() -> None:
    """Each skill fits alone; the one that would overrun the session's total is a named loss."""
    import uuid

    from daimon.core.stores.domain import SkillSizeRow
    from daimon.core.turn.session_skills import fitting_skills, skill_losses, skill_names
    from mux.contracts.ids import SkillRef

    profile = PROFILES["gemini.inline_reuse"]
    pins = tuple(SkillRef(id=f"s{i}", version="1") for i in range(3))
    sizes = {
        (pin.id, "1"): SkillSizeRow(
            tenant_id=uuid.uuid4(),
            skill_id=pin.id,
            version="1",
            name=f"skill-{pin.id}",
            total_bytes=900 * 1024,
            largest_file_bytes=900 * 1024,
            file_count=1,
            text_only=True,
        )
        for pin in pins
    }
    kept = fitting_skills(profile, pins, sizes)
    assert kept == pins[:2], "two fit in 2 MiB, the third does not"
    assert skill_losses(pins, kept) == ("skill:s2",), "named, never silent"
    assert skill_losses(pins, ()) == tuple(f"skill:{p.id}" for p in pins), (
        "a skill measured or not is the same loss, so a consent holds across captures"
    )
    assert skill_names(sizes) == {p.id: f"skill-{p.id}" for p in pins}, "names for the card"


def test_a_skill_loss_shows_its_name_on_every_surface() -> None:
    """The drop keys a skill by id; the card, its words and the preview show the name."""
    import uuid

    from daimon.core.channel_backend import compatibility_preview, switch_losses
    from daimon.core.confirmation import drop_words
    from daimon.core.stores.domain import BackendAgent, SourceRepresentation, TenantBackendPolicy
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec

    target = AgentSpec(name="helper", model=ModelRef(provider="gemini", id="gemini-x"))
    agent = BackendAgent(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="helper",
        configuration_digest="config",
        representations=(
            SourceRepresentation(
                source_pin="0" * 64, target=target, skill_names={"skill_01a": "pdf-tools"}
            ),
        ),
    )
    drops = ("skill:skill_01a", "skill:skill_01b")
    assert switch_losses(drops, agent)[0] == ("pdf-tools", "skill_01b"), "named, else its id"
    policy = TenantBackendPolicy(
        tenant_id=agent.tenant_id, enabled_backends=("gemini",), revision="r"
    )
    preview = compatibility_preview(agent, "gemini", policy, declared_drops=drops)
    assert preview.unsupported_skills == ("pdf-tools", "skill_01b"), "the card shows the name"
    assert drop_words(drops, preview.skill_names) == (
        "its pdf-tools skill and its skill_01b skill"
    ), "the card's words show the name"

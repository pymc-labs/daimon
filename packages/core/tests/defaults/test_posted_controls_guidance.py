from __future__ import annotations

import re
from pathlib import Path

from daimon.core.defaults.loader import load_agent_specs, load_skill_paths, load_skill_spec

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULTS = REPO_ROOT / "defaults"

# The posted card (both Discord and Slack) already states the target, the
# requester restriction, the expiry, and who can use the result afterwards.
# The conversational reply must not restate any of these card facts.
FORBIDDEN_CARD_FACTS = (
    "expires in 30 minutes",
    "Only the requester",
    "only you can open",
    "Anyone who talks to",
)


def _daimon_system() -> str:
    specs = load_agent_specs(DEFAULTS / "agents")
    daimon = next(s for s in specs if s.name == "daimon")
    system = daimon.system or ""
    assert system, "seeded daimon agent must have a system prompt"
    return system


def _workspace_setup_body() -> str:
    for skill_dir in load_skill_paths(DEFAULTS / "skills"):
        spec, body = load_skill_spec(skill_dir)
        if spec.name == "workspace-setup":
            return body
    raise AssertionError("defaults/skills/workspace-setup must exist")


def _section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def test_daimon_key_only_reply_shape_does_not_duplicate_card_facts() -> None:
    """The key-only reply shape must point at the card, not restate it."""
    system = _daimon_system()
    reply_shape = _section(system, "Key-only request:", "\n\n")
    for phrase in FORBIDDEN_CARD_FACTS:
        assert phrase not in reply_shape, (
            f"daimon.yaml key-only reply shape restates card fact {phrase!r}; "
            "the posted card already carries it"
        )


def test_workspace_setup_keys_reply_shape_does_not_duplicate_card_facts() -> None:
    """The Keys step's key-only guidance must point at the card, not restate it."""
    body = _workspace_setup_body()
    keys_section = _section(body, "2. **Keys.**", "3. **Skills.**")
    for phrase in FORBIDDEN_CARD_FACTS:
        assert phrase not in keys_section, (
            f"workspace-setup Keys section restates card fact {phrase!r}; "
            "the posted card already carries it"
        )


def test_guidance_names_pending_task_and_bind_public_repo() -> None:
    """The model must be told to pass pending_task and to use bind_public_repo."""
    combined = _daimon_system() + "\n" + _workspace_setup_body()
    assert "pending_task" in combined, (
        "guidance must tell the model to pass pending_task so a waiting task resumes "
        "on its own after the value is saved"
    )
    assert "bind_public_repo" in combined, (
        "guidance must name bind_public_repo for a named agent's public GitHub repo"
    )


def test_skill_repo_guidance_no_longer_claims_a_working_repo_change() -> None:
    """Skill-repo separation landed: importing skills must not claim to touch the working repo."""
    body = _workspace_setup_body()
    skills_section = _section(body, "3. **Skills.**", "4. **MCP servers.**")
    for phrase in ("also binds the target's working repo", "working-repo binding"):
        assert phrase not in skills_section, (
            f"skill-repo guidance still claims {phrase!r}; a skill-repo token no longer "
            "rewrites the working repo binding"
        )
    assert "never changes the working repo" in skills_section, (
        "skill-repo guidance must say importing skills never changes the working repo"
    )


def test_after_confirmed_save_notes_next_message_availability_and_card_result() -> None:
    """The after-save paragraph must not claim a saved key works immediately or need paraphrasing."""
    body = _workspace_setup_body()
    save_section = _section(body, "After a confirmed save,", "## Which agent answers where")
    assert "next message" in save_section, (
        "guidance must say a saved key becomes usable from the next message, not this one"
    )
    assert "paraphrase" in save_section, (
        "guidance must say not to paraphrase the card's result line"
    )


def _reply_shape_sections() -> list[tuple[str, str]]:
    """The two places that tell the model what to say after posting a card."""
    return [
        (
            "daimon.yaml key-only reply shape",
            _section(_daimon_system(), "Key-only request:", "\n\n"),
        ),
        (
            "workspace-setup Keys section",
            _section(_workspace_setup_body(), "2. **Keys.**", "3. **Skills.**"),
        ),
    ]


def test_reply_shape_points_below_the_reply_and_never_above() -> None:
    """The card is always posted after the reply, so the pointer must say below."""
    for label, section in _reply_shape_sections():
        assert "form below" in section, (
            f"{label} must point the person to the form below; the card lands after the reply"
        )
        unquoted = [
            match for match in re.finditer("above", section) if section[match.start() - 1] != '"'
        ]
        assert not unquoted, (
            f"{label} says 'above' outside the prohibition; the card is never above the reply"
        )


def test_reply_shape_never_asks_the_model_to_state_the_expiry() -> None:
    """D24-QA-04/S24-01: the card carries a live timestamp, the prose goes stale."""
    for label, section in _reply_shape_sections():
        assert "expires" not in section, (
            f"{label} uses the word 'expires'; the reply must never restate when the card expires"
        )


def test_reply_shape_covers_the_waiting_task_path() -> None:
    """The `pending_task` branch is where the reply ran long, so it is named explicitly."""
    for label, section in _reply_shape_sections():
        assert "pending_task" in section, (
            f"{label} must cover the waiting-task path: the continuation resumes on its own, "
            "so the reply does not describe what happens after the save"
        )


def test_guidance_resolves_named_agents_independently_of_the_shared_bot_handle() -> None:
    """The bot's display name must not override a requested roster identity."""
    for label, text in (
        ("daimon.yaml", _daimon_system()),
        ("workspace-setup", _workspace_setup_body()),
    ):
        assert "responder.handle" in text, f"{label} must identify the shared bot handle"
        assert "Multiple agents use the same bot handle" in text, (
            f"{label} must distinguish the bot from its roster agents"
        )
        assert "`list_agents` and `get_agent`" in text, (
            f"{label} must resolve explicit agent choices against the roster"
        )
        assert "`list_agent_keys`" in text, f"{label} must use stored-key metadata"
        assert "are one agent" not in text, f"{label} must not collapse distinct identities"


def test_reply_shape_posts_the_card_before_any_clarifying_question() -> None:
    """D24-QA-06: a key named inside a larger task must still post the card first."""
    for label, section in _reply_shape_sections():
        assert "before any clarifying question" in section, (
            f"{label} must say the request tool is called before any clarifying question; "
            "an underspecified surrounding task is not a reason to withhold the card"
        )
        assert "underspecified" in section, (
            f"{label} must say the form is posted even when the waiting task is underspecified"
        )
        assert "never replaces the card" in section, (
            f"{label} must say a clarifying question comes after the card and never replaces it"
        )


def test_reply_shape_scopes_pending_task_to_work_that_runs_after_the_save() -> None:
    """A save-only request ends at the card: no pending_task, so no billed continuation."""
    for label, section in _reply_shape_sections():
        assert "omit `pending_task`" in section, (
            f"{label} must tell the model to omit pending_task when the person only asked "
            "to add, save or replace a key and named no further work"
        )
        assert "Never put the key request itself in `pending_task`" in section, (
            f"{label} must forbid passing the key request back as the waiting task"
        )
        assert "nothing runs after the save" in section, (
            f"{label} must say a save-only request ends at the card with nothing running after it"
        )


def test_reply_shape_keeps_tool_names_out_of_the_reply() -> None:
    """D24-QA-07: `request_agent_key` reached the person as an instruction in prose."""
    for label, section in _reply_shape_sections():
        assert "Never name a tool" in section, (
            f"{label} must forbid naming a tool in the reply; tools are how the agent acts, "
            "not what it says"
        )


def test_guidance_drops_editor_era_and_duplicate_routing_phrases() -> None:
    """Setup-orientation repetition audit (PR 4).

    The read-only panel's New agent form lands on Details — there is no
    separate success screen to name — and a newly created or forked agent's
    routing state is relayed from the tool's own ``answering`` field rather
    than restated in independently worded prose.
    """
    combined = _daimon_system() + "\n" + _workspace_setup_body()
    for phrase in ("new-agent success control", "Creation alone does not route mentions"):
        assert phrase not in combined, f"guidance still contains the removed phrase {phrase!r}"


def test_daimon_system_skips_orientation_after_the_setup_opener() -> None:
    """The setup thread's opener already asks what to configure; the
    general unconfigured-workspace orientation must not repeat it."""
    system = _daimon_system()
    assert "setup opener already asked what to configure" in system, (
        "daimon.yaml must tell the model to skip the general orientation when "
        "the setup conversation opener already covered it"
    )


def test_reply_shape_forbids_describing_a_form_that_was_not_posted() -> None:
    """D24-QA-06: the agent told the person about a key-request form it never posted."""
    for label, section in _reply_shape_sections():
        assert "a form you did not post" in section, (
            f"{label} must forbid describing, promising or referring to a form you did "
            "not post in this turn"
        )
        assert "if posting failed, say" in section, (
            f"{label} must tell the model to say what failed when posting fails"
        )


def test_guidance_says_to_post_the_answering_and_applies_sentences_word_for_word() -> None:
    """D25-QA-01/S25-01/S25-02: three chat creations paraphrased the routing
    sentence the tool returned, and one of the paraphrases invented a
    channel-specific routing step no tool had supplied."""
    system = _daimon_system()
    assert "`answering` or `applies`" in system, (
        "daimon.yaml must name both tool-result sentences the reply carries unchanged"
    )
    assert "word for word" in system, (
        "daimon.yaml must tell the model to post those sentences word for word, "
        "not to paraphrase them"
    )
    assert "no routing step the tool did not give" in system, (
        "daimon.yaml must forbid adding a routing step the tool result did not supply"
    )


def test_workspace_setup_says_a_setup_target_refusal_does_not_block_configuration() -> None:
    """D25-QA-02: one `set_setup_target` refusal in ordinary chat convinced the
    model it could not configure the named agent at all, and the belief carried
    into the next turn."""
    body = _workspace_setup_body()
    section = _section(body, "## Permissions and refusals", "An operator-only problem")
    assert "`set_setup_target` refusal" in section, (
        "workspace-setup must say what a set_setup_target refusal actually means"
    )
    assert "never means the agent cannot be configured" in section, (
        "workspace-setup must say the refusal does not block configuration"
    )
    assert "Configure it by name" in section, (
        "workspace-setup must name the way through: configure the agent by name"
    )

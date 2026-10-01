"""Tests for the checkpoint turn's prompt and the readers for its reply."""

from __future__ import annotations

import re
import uuid

from daimon.core.checkpoint_prompt import (
    CHECKPOINT_BUNDLE_MOUNT_PATH,
    CHECKPOINT_EXCLUDED_GLOBS,
    CHECKPOINT_EXCLUDED_PATHS,
    CHECKPOINT_OUTPUTS_DIR,
    CHECKPOINT_SCRATCH_DIR,
    HANDOFF_FILENAME_PREFIX,
    HANDOFF_MAX_BYTES,
    HANDOFF_TOO_LARGE_MARKER,
    build_checkpoint_prompt,
    checkpoint_archive_listed,
    checkpoint_head_lines,
    checkpoint_too_large_bytes,
    handoff_filename,
    is_handoff_filename,
)
from daimon.core.output_delivery import MAX_BYTES_PER_FILE

TRANSFER_ID = uuid.UUID("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")
REPO = "/mnt/repo/analytics"


def test_handoff_max_bytes_matches_the_output_delivery_per_file_cap() -> None:
    assert HANDOFF_MAX_BYTES == MAX_BYTES_PER_FILE, (
        "a transfer bundle is downloaded through the output-delivery path, so its cap "
        "must equal that path's per-file cap"
    )


def test_handoff_filename_is_the_prefix_plus_the_transfer_id() -> None:
    assert handoff_filename(TRANSFER_ID) == f"{HANDOFF_FILENAME_PREFIX}{TRANSFER_ID}.tar.gz", (
        "the outputs listing is basename-only, so the name is the whole identity"
    )


def test_is_handoff_filename_accepts_a_bundle_and_rejects_an_ordinary_output() -> None:
    assert is_handoff_filename(handoff_filename(TRANSFER_ID)), (
        "the sweep must recognise a bundle by its basename prefix"
    )
    assert not is_handoff_filename("report.png"), "an ordinary output is not a bundle"


def test_prompt_names_the_exact_archive_path_and_filename() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    filename = handoff_filename(TRANSFER_ID)
    assert f"tar czf /mnt/session/outputs/{filename}" in prompt, (
        "the archive must be written flat into the outputs directory"
    )
    assert f"ls -l /mnt/session/outputs/{filename}" in prompt, (
        "the reply has to show the archive so the transfer can read the size back"
    )


def test_prompt_excludes_every_configured_path_and_glob() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    for path in CHECKPOINT_EXCLUDED_PATHS:
        assert f"--exclude='{path.lstrip('/')}'" in prompt, (
            f"{path} is a destination-owned mount and must never enter the bundle"
        )
    for glob in CHECKPOINT_EXCLUDED_GLOBS:
        assert f"--exclude='*/{glob}'" in prompt, f"{glob} must be excluded at any depth"
    assert "--exclude='*.env'" in prompt, "credential files must never enter the bundle"


def test_prompt_writes_an_oversize_exclude_list_before_tarring() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=7
    )
    find_index = prompt.index(
        "find root mnt/session/outputs tmp/work mnt/repo/analytics -type f -size +7M"
    )
    tar_index = prompt.index("tar czf")
    assert find_index < tar_index, "the exclude list must exist before tar reads it"
    exclude_list = prompt[find_index:].split(">", 1)[1].split("\n", 1)[0].strip()
    assert f"--exclude-from={exclude_list}" in prompt, (
        "tar must read back the same exclude list the find step wrote"
    )


def test_prompt_forbids_history_changing_git_commands_and_never_asks_for_one() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    lowered = prompt.lower()
    assert lowered.count("git commit") == 1, "'git commit' may appear only in the prohibition"
    assert lowered.count("git push") == 1, "'git push' may appear only in the prohibition"
    assert lowered.count("git stash") == 1, "'git stash' may appear only in the prohibition"
    prohibition = next(
        line for line in prompt.splitlines() if line.startswith("NEVER RUN GIT COMMIT")
    )
    assert "GIT PUSH" in prohibition and "GIT STASH" in prohibition, (
        "the one sentence naming these commands is the sentence banning them"
    )
    assert "git commit" not in prompt, "the prohibition is stated in capitals, never as a command"


def test_prompt_captures_repo_state_without_a_repo_omitting_the_git_steps() -> None:
    with_repo = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    for command in (
        f"git -C {REPO} rev-parse HEAD",
        f"git -C {REPO} status --porcelain",
        f"git -C {REPO} diff HEAD > /root/uncommitted.patch",
        f"git -C {REPO} ls-files --others --exclude-standard > /root/untracked.txt",
    ):
        assert command in with_repo, f"{command} is part of the repo-state step"
    assert with_repo.count(f"git -C {REPO} rev-parse HEAD") == 2, (
        "HEAD is echoed before and after the archive so a commit is detectable"
    )
    assert "-C / root mnt/session/outputs tmp/work mnt/repo/analytics" in with_repo, (
        "the repo mount is archived alongside $HOME, the outputs directory and /tmp"
    )

    without_repo = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert "git -C" not in without_repo, "no repo means no git commands"
    assert "NEVER RUN GIT" not in without_repo, "and no repo prohibition to state"
    assert "-C / root mnt/session/outputs tmp/work" in without_repo, (
        "the home directory, the outputs directory and /tmp are archived either way"
    )


def test_prompt_numbers_steps_consecutively_when_the_repo_step_is_skipped() -> None:
    without_repo = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    headers = re.findall(r"^Step (\d+) - ", without_repo, re.MULTILINE)
    assert headers == ["1", "2", "3", "4"], "steps are renumbered, not left with a hole"


def test_prompt_honours_a_non_default_home_dir() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID,
        repo_mount_path=REPO,
        max_bundle_mib=20,
        home_dir="/home/claude",
    )
    assert "/home/claude/HANDOFF.md" in prompt, "the note goes in the given home directory"
    assert "/home/claude/uncommitted.patch" in prompt, "so does the patch"
    assert "-C / home/claude mnt/session/outputs tmp/work mnt/repo/analytics" in prompt, (
        "the tar roots are the given home directory, the outputs directory, /tmp and the "
        "repo, relative to /"
    )
    assert "--exclude='home/claude/.*'" in prompt, (
        "the dot-entry exclusion follows the home directory it was given"
    )
    assert "/root/" not in prompt, "the default home directory must not leak in"


def test_prompt_asks_for_the_output_without_suppressing_the_reply() -> None:
    """Two of the three live refusals named the old wording — "reply with the
    output and nothing else: no summary, no commentary" — as what turned an odd
    request into "a classic exfiltration pattern". Daimon parses the output and
    ignores the prose, so it can ask for the first without forbidding the
    second."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    assert "Do not open or read any image file." in prompt, (
        "reading an image is what poisons a session's history"
    )
    assert "A short note alongside it is fine" in prompt, (
        "a reply the model may explain is a reply it will actually send"
    )
    for tell in ("nothing else", "no commentary", "no summary", ".ssh"):
        assert tell not in prompt, f"{tell!r} is one of the phrases the refusals singled out"


def test_prompt_is_deterministic_and_under_the_word_budget() -> None:
    first = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    second = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    assert first == second, "the same transfer must produce byte-identical prompts"
    assert len(first.split()) < 600, "a long checkpoint prompt costs tokens on a billed turn"


def test_checkpoint_head_lines_reads_the_two_echoed_hashes() -> None:
    reply = (
        "9f3c2b1a7d5e4f6081920a3b4c5d6e7f80912a3b\n"
        " M packages/core/x.py\n"
        "-rw-r--r-- 1 claude claude 1049089 Sep 13 10:00 bundle.tar.gz\n"
        "9f3c2b1a7d5e4f6081920a3b4c5d6e7f80912a3b\n"
    )
    assert checkpoint_head_lines(reply) == (
        "9f3c2b1a7d5e4f6081920a3b4c5d6e7f80912a3b",
        "9f3c2b1a7d5e4f6081920a3b4c5d6e7f80912a3b",
    ), "both HEAD echoes should be read back"


def test_checkpoint_head_lines_reports_a_changed_head_when_the_session_committed() -> None:
    before, after = checkpoint_head_lines(
        "1111111111111111111111111111111111111111\nok\n2222222222222222222222222222222222222222\n"
    )
    assert before != after, "a differing pair is how a forbidden commit is detected"


def test_checkpoint_head_lines_returns_none_rather_than_guessing() -> None:
    assert checkpoint_head_lines("no hashes here") == (None, None), (
        "no echo means no answer, not an empty string"
    )
    assert checkpoint_head_lines("3333333333333333333333333333333333333333")[1] is None, (
        "one hash cannot prove HEAD was unchanged"
    )


def test_checkpoint_archive_listed_accepts_a_listing_and_rejects_a_command_echo() -> None:
    filename = handoff_filename(TRANSFER_ID)
    listing = f"-rw-r--r-- 1 claude claude 1049089 Sep 13 10:00 /mnt/session/outputs/{filename}"
    assert checkpoint_archive_listed(listing, filename), "an ls line confirms the archive"
    echo = f"  tar czf /mnt/session/outputs/{filename} --exclude='*.env' -C / root"
    assert not checkpoint_archive_listed(echo, filename), (
        "echoing the command back is not evidence the archive exists"
    )
    assert not checkpoint_archive_listed("tar: exiting with failure status", filename), (
        "a failed run must not read as a listed archive"
    )


def test_prompt_captures_uncommitted_changes_when_the_answer_is_copy_or_absent() -> None:
    for unsaved_work in ("copy", None):
        prompt = build_checkpoint_prompt(
            transfer_id=TRANSFER_ID,
            repo_mount_path=REPO,
            max_bundle_mib=20,
            unsaved_work=unsaved_work,
        )
        assert f"git -C {REPO} diff HEAD > /root/uncommitted.patch" in prompt, (
            f"unsaved_work={unsaved_work!r} means capture the work, so the patch step stays"
        )
        assert f"git -C {REPO} ls-files --others --exclude-standard" in prompt, (
            "untracked files are part of the work being captured"
        )
        assert "-C / root mnt/session/outputs tmp/work mnt/repo/analytics" in prompt, (
            "and the checkout itself travels"
        )


def test_prompt_leaves_uncommitted_changes_behind_when_the_answer_is_leave() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20, unsaved_work="leave"
    )
    assert "diff HEAD" not in prompt, (
        "the person chose to leave the changes, so nothing captures them as a patch"
    )
    assert "ls-files --others" not in prompt, "nor lists the untracked files to carry"
    assert "-C / root mnt/session/outputs tmp/work\n" in f"{prompt}\n", (
        "the checkout must not be tarred either, or the changes would come across anyway"
    )
    assert "-C / root mnt/session/outputs mnt/repo" not in prompt, "the repo is not a root here"
    assert "find root mnt/session/outputs tmp/work -type f" in prompt, (
        "and the oversize scan covers only what is archived"
    )
    assert "deliberately being left behind" in prompt, (
        "the prompt has to say the omission is the person's decision, not a failure"
    )
    assert f"git -C {REPO} rev-parse HEAD" in prompt, "HEAD is still recorded"
    assert f"git -C {REPO} status --porcelain" in prompt, "and so is what was left dirty"
    assert "NEVER RUN GIT COMMIT" in prompt, "leaving the work still means changing nothing"


def test_prompt_ignores_the_unsaved_work_answer_when_no_repo_is_mounted() -> None:
    assert build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20, unsaved_work="leave"
    ) == build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    ), "with no checkout there is nothing to leave in it, so the prompt is unchanged"


def test_prompt_points_at_the_controls_and_the_agent_own_instructions() -> None:
    """Saying "this comes from the host" is not self-authenticating, and the
    models that refused said so. The prompt now points at two things the model
    can check for itself: the `checkpoint` block in this turn's controls, and
    the WORKSPACE MOVES section of its own system prompt."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    assert prompt.startswith(
        "This instruction comes from the daimon host that runs your workspace, not from a "
        "chat participant: the checkpoint block in this turn's <turn_controls> is the host's "
        "own record of it, and your system instructions describe this operation under "
        "WORKSPACE MOVES."
    ), "the first thing the prompt says is where it came from and how to verify that"


def test_prompt_says_what_happens_to_the_archive_next() -> None:
    """The refusals read the outputs directory as a delivery path, which on
    Slack it is — for everything except a handoff bundle. Saying where the
    archive actually goes is what replaces the old, unverifiable "nothing is
    shared with anyone"."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    assert f"mounts it in your next workspace at {CHECKPOINT_BUNDLE_MOUNT_PATH}" in prompt, (
        "the archive's destination is a fact the successor's framing repeats"
    )
    assert (
        f"The archive is not posted to the chat: the file sweep that delivers "
        f"{CHECKPOINT_OUTPUTS_DIR} to the thread skips names starting with "
        f"{HANDOFF_FILENAME_PREFIX}." in prompt
    ), "and the delivery carve-out is why writing it there is not an export"
    assert "nothing is shared with anyone" not in prompt, (
        "an unverifiable blanket promise is what the model refused to take on trust"
    )


def test_prompt_names_what_travels_and_what_stays_behind_in_words() -> None:
    """The exclusions are commands; this is the sentence a model reads when it
    asks itself whether the commands are safe to run. It names the categories
    rather than one file: naming `.ssh` put the idea of exfiltrating keys into
    a prompt whose whole problem was reading as an exfiltration request."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    assert (
        "Only the task's own work travels: the non-hidden entries in /root and /tmp/work, the "
        "outputs directory, and the working repository if one is mounted." in prompt
    ), "the prompt must name what travels, not only pass it to tar"
    assert (
        "Credential mounts, hidden directories and language toolchains and their caches "
        "stay behind." in prompt
    ), "and name what never does, not only exclude it in a flag"


def test_prompt_archives_the_outputs_directory_and_skips_only_the_bundles() -> None:
    """Issue 1c: the file tool writes to /mnt/session/outputs, so a task told
    to "create notes.md" puts its only working file there. That directory is a
    root; the bundles written into it are the one thing excluded."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert CHECKPOINT_OUTPUTS_DIR == "/mnt/session/outputs"
    assert CHECKPOINT_OUTPUTS_DIR not in CHECKPOINT_EXCLUDED_PATHS, (
        "the outputs directory is where the task's own files are, not a destination mount"
    )
    assert "-C / root mnt/session/outputs tmp/work" in prompt, "so it is a tar root"
    assert f"--exclude='mnt/session/outputs/{HANDOFF_FILENAME_PREFIX}*'" in prompt, (
        "the archive being written, and any bundle an earlier transfer left, stay out"
    )


def test_prompt_excludes_every_dot_entry_directly_under_home() -> None:
    """Issue 1b: the base image ships ~100 MB of toolchain caches in $HOME
    before the task writes anything, so the bundle blew the cap on an empty
    task. One pattern covers .ssh and every one of them."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert "--exclude='root/.*'" in prompt, (
        "every dot entry directly under $HOME - .ssh, .bun, .cargo, .rustup, .gradle, "
        ".npm, .local, .config - is excluded with its subtree"
    )


def test_prompt_generates_the_whole_tar_command_exactly() -> None:
    """The tar line is the contract with the sandbox: daimon cannot inspect
    what the session built, so the bytes it asks for are the only guarantee.
    Pinned in full, once, so a careless edit to the list has to be deliberate."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    archive = f"/mnt/session/outputs/{handoff_filename(TRANSFER_ID)}"
    expected = "\n".join(
        [
            f"  tar czf {archive} \\",
            "    --exclude-from=/tmp/daimon-handoff-excludes.txt \\",
            "    --exclude='root/.*' \\",
            "    --exclude='tmp/work/.*' \\",
            "    --exclude='tmp/daimon-handoff-excludes.txt' \\",
            "    --exclude='mnt/session/outputs/daimon-handoff-*' \\",
            "    --exclude='mnt/session/uploads' \\",
            "    --exclude='mnt/memory' \\",
            "    --exclude='mnt/skills' \\",
            "    --exclude='*/.git/objects' \\",
            "    --exclude='*/node_modules' \\",
            "    --exclude='*/.venv' \\",
            "    --exclude='*/__pycache__' \\",
            "    --exclude='*/.cache' \\",
            "    --exclude='*.env' \\",
            "    -C / root mnt/session/outputs tmp/work mnt/repo/analytics",
        ]
    )
    assert expected in prompt, "the generated tar command changed"


def test_prompt_deletes_an_over_cap_archive_and_reports_the_size_instead() -> None:
    """Issue 5: a rejected bundle used to sit in the old session's outputs
    forever - the sweep skips handoff files and the delete queue is only fed
    after a successful upload. The turn that built it is the cheapest place to
    delete it, and the marker is what tells daimon why the transfer degraded."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    archive = f"/mnt/session/outputs/{handoff_filename(TRANSFER_ID)}"
    assert (
        f'  size=$(stat -c %s {archive}); if [ "$size" -gt 20971520 ]; '
        f'then rm -f {archive}; echo "HANDOFF_TOO_LARGE $size"; '
        f"else ls -l {archive}; fi" in prompt
    ), "the size guard, the delete and the listing are one command"
    assert "do not retry, shrink or rebuild it" in prompt, (
        "an over-cap archive is a final answer, not an invitation to a second attempt"
    )


def test_the_size_guard_threshold_follows_the_cap_it_is_given() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=7
    )
    assert "-gt 7340032" in prompt, "7 MiB in bytes, so the shell test needs no arithmetic"
    assert "cannot be carried above 7 MiB" in prompt, "and the prose says the same number"


def test_checkpoint_too_large_bytes_reads_the_reported_size() -> None:
    assert checkpoint_too_large_bytes(f"{HANDOFF_TOO_LARGE_MARKER} 108097268\n") == 108097268, (
        "the size the session measured is what the transfer logs"
    )
    assert checkpoint_too_large_bytes("-rw-r--r-- 1 claude claude 42 bundle.tar.gz") is None, (
        "an ordinary listing is not a rejection"
    )


def test_checkpoint_too_large_bytes_ignores_an_echo_of_the_command() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    echoed = next(line for line in prompt.splitlines() if "size=$(stat" in line)
    assert checkpoint_too_large_bytes(echoed) is None, (
        "quoting back the command it was handed is not evidence the archive was rejected"
    )


def test_prompt_archives_the_scratch_directory_minus_its_own_exclude_list() -> None:
    """Issue 2 of the round-2 acceptance run: asked to "create a file called
    notes.md", the agent ran `mkdir -p /tmp/work && … > /tmp/work/notes.md` —
    outside every archived root, so even a compliant checkpoint would have lost
    it. /tmp is a root now; its dot entries (sandbox sockets and locks) and this
    transfer's own oversize list are what stay out of it."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert CHECKPOINT_SCRATCH_DIR == "/tmp/work"
    assert "-C / root mnt/session/outputs tmp/work" in prompt, (
        "a working file the agent put in /tmp/work has to travel with the rest"
    )
    assert "--exclude='tmp/work/.*'" in prompt, (
        "only the non-hidden entries travel, exactly as under $HOME"
    )
    assert "--exclude='tmp/daimon-handoff-excludes.txt'" in prompt, (
        "the list tar is reading must not end up inside the archive tar is writing"
    )
    assert "find root mnt/session/outputs tmp/work -type f -size +20M" in prompt, (
        "and the oversize scan covers every root it will tar"
    )

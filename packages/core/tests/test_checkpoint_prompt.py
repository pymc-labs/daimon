"""Tests for the checkpoint turn's prompt and the readers for its reply."""

from __future__ import annotations

import random
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from daimon.core.checkpoint_prompt import (
    CHECKPOINT_BUNDLE_MOUNT_PATH,
    CHECKPOINT_EXCLUDED_GLOBS,
    CHECKPOINT_EXCLUDED_PATHS,
    CHECKPOINT_OUTPUTS_DIR,
    CHECKPOINT_SCRATCH_DIR,
    HANDOFF_FILENAME_PREFIX,
    HANDOFF_LEFT_OUT_MARKER,
    HANDOFF_LEFT_OUT_MORE_MARKER,
    HANDOFF_MAX_BYTES,
    HANDOFF_TOO_LARGE_MARKER,
    build_checkpoint_prompt,
    checkpoint_head_lines,
    checkpoint_left_out,
    checkpoint_too_large_bytes,
    handoff_filename,
    is_handoff_filename,
)
from daimon.core.handoff_context import render_handoff_framing
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
    assert f"tar czf /mnt/session/outputs/{filename}.part " in prompt, (
        "the archive must be written flat into the outputs directory, under a partial name"
    )
    assert f"mv /mnt/session/outputs/{filename}.part /mnt/session/outputs/{filename};" in prompt, (
        "and renamed once it is done, so a killed turn leaves no truncated bundle"
    )
    assert f"ls -l /mnt/session/outputs/{filename}" in prompt, (
        "the reply has to show the archive so the transfer can read the size back"
    )


def test_prompt_excludes_every_configured_path_and_glob() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    for path in CHECKPOINT_EXCLUDED_PATHS:
        assert f"-path '{path.lstrip('/')}'" in prompt, (
            f"{path} is a destination-owned mount and must never enter the bundle"
        )
    for glob in CHECKPOINT_EXCLUDED_GLOBS:
        assert f"-path '*/{glob}'" in prompt, f"{glob} must be excluded at any depth"
    assert "-path '*.env'" in prompt, "credential files must never enter the bundle"


def test_prompt_leaves_out_files_over_the_cap_from_what_travels() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=7
    )
    assert (
        "members() { find . root mnt/session/outputs tmp/work mnt/repo/analytics -mindepth 1 "
        "\\( -path './*' ! -type f -o -path './.*' -o -type f -size +7M" in prompt
    ), "one find lists what travels, never a file too large to carry"
    assert "  pack() { members -printf '%p\\0' | tar czf" in prompt, (
        "tar packs exactly what that find lists"
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
    assert "find . root mnt/session/outputs tmp/work mnt/repo/analytics " in with_repo, (
        "the repo mount is archived alongside $HOME, the outputs directory and /tmp"
    )

    without_repo = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert "git -C" not in without_repo, "no repo means no git commands"
    assert "NEVER RUN GIT" not in without_repo, "and no repo prohibition to state"
    assert "find . root mnt/session/outputs tmp/work " in without_repo, (
        "the home directory, the outputs directory and /tmp are archived either way"
    )


def test_prompt_numbers_steps_consecutively_when_the_repo_step_is_skipped() -> None:
    without_repo = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    headers = re.findall(r"^Step (\d+) - ", without_repo, re.MULTILINE)
    assert headers == ["1", "2", "3"], "steps are renumbered, not left with a hole"


def test_prompt_honours_a_non_default_home_dir() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID,
        repo_mount_path=REPO,
        max_bundle_mib=20,
        home_dir="/home/claude",
    )
    assert "/home/claude/HANDOFF.md" in prompt, "the note goes in the given home directory"
    assert "/home/claude/uncommitted.patch" in prompt, "so does the patch"
    assert "find . home/claude mnt/session/outputs tmp/work mnt/repo/analytics " in prompt, (
        "the archived roots are the given home directory, the outputs directory, /tmp and the "
        "repo, relative to /"
    )
    assert "-path 'home/claude/.*'" in prompt, (
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
    # The partial rebuild added ~100 words of commands; the prose stays lean.
    assert len(first.split()) < 720, "a long checkpoint prompt costs tokens on a billed turn"


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
        assert "find . root mnt/session/outputs tmp/work mnt/repo/analytics " in prompt, (
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
    assert "find . root mnt/session/outputs tmp/work -mindepth 1 " in prompt, (
        "the checkout must not be tarred either, or the changes would come across anyway"
    )
    assert "mnt/repo/analytics -mindepth" not in prompt, "the repo is not a root here"
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
        "files directly in / (where the shell starts), the outputs directory, and the working repository if one is mounted."
        in prompt
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
    assert "find . root mnt/session/outputs tmp/work " in prompt, "so it is an archived root"
    assert f"-path 'mnt/session/outputs/{HANDOFF_FILENAME_PREFIX}*'" in prompt, (
        "the archive being written, and any bundle an earlier transfer left, stay out"
    )


def test_prompt_excludes_every_dot_entry_directly_under_home() -> None:
    """Issue 1b: the base image ships ~100 MB of toolchain caches in $HOME
    before the task writes anything, so the bundle blew the cap on an empty
    task. One pattern covers .ssh and every one of them."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert "-path 'root/.*'" in prompt, (
        "every dot entry directly under $HOME - .ssh, .bun, .cargo, .rustup, .gradle, "
        ".npm, .local, .config - is excluded with its subtree"
    )


def test_prompt_generates_the_whole_tar_command_exactly() -> None:
    """The find and tar lines are the contract with the sandbox: daimon cannot
    inspect what the session built, so the bytes it asks for are the only
    guarantee. Pinned in full, once, so a careless edit has to be deliberate."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    archive = f"/mnt/session/outputs/{handoff_filename(TRANSFER_ID)}.part"
    expected = "\n".join(
        [
            "  members() { find . root mnt/session/outputs tmp/work mnt/repo/analytics -mindepth 1 "
            "\\( -path './*' ! -type f -o -path './.*' -o -type f -size +20M \\",
            "    -o -path 'root/.*' -o -path 'tmp/work/.*' "
            "-o -path 'mnt/session/outputs/daimon-handoff-*' \\",
            "    -o -path 'mnt/session/uploads' -o -path 'mnt/memory' -o -path 'mnt/skills' \\",
            "    -o -path '*/.git/objects' -o -path '*/node_modules' -o -path '*/.venv' "
            "-o -path '*/__pycache__' -o -path '*/.cache' -o -path '*.env' \\",
            '    \\) -prune -o "$@"; }',
            f"  pack() {{ members -printf '%p\\0' | tar czf {archive} --no-recursion --anchored "
            "--no-wildcards --exclude-from=/tmp/daimon-handoff-left-out.txt --null -T -; }",
            "  pack",
        ]
    )
    assert expected in prompt, "the generated find and tar commands changed"


def test_prompt_deletes_an_over_cap_archive_and_reports_the_size_instead() -> None:
    """Issue 5: a rejected bundle used to sit in the old session's outputs
    forever - the sweep skips handoff files and the delete queue is only fed
    after a successful upload. The turn that built it is the cheapest place to
    delete it, and the marker is what tells daimon why the transfer degraded."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    archive = f"/mnt/session/outputs/{handoff_filename(TRANSFER_ID)}"
    partial = f"{archive}.part"
    assert f'  size=$(stat -c %s {partial}); if [ "$size" -gt 20971520 ]; then\n' in prompt, (
        "an over-cap archive is rebuilt first"
    )
    assert (
        f'  if [ "$size" -gt 20971520 ]; then rm -f {partial}; echo "HANDOFF_TOO_LARGE $size"; '
        f"else mv {partial} {archive}; ls -l {archive}; fi" in prompt
    ), "the size guard, the delete and the listing are one command, after the rebuild"
    assert "do not retry, shrink or rebuild the archive yourself" in prompt, (
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


def test_prompt_archives_the_scratch_directory_minus_its_dot_entries() -> None:
    """Issue 2 of the round-2 acceptance run: asked to "create a file called
    notes.md", the agent ran `mkdir -p /tmp/work && … > /tmp/work/notes.md` —
    outside every archived root, so even a compliant checkpoint would have lost
    it. /tmp/work is a root now; its dot entries (sandbox sockets and locks)
    stay out of it."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert CHECKPOINT_SCRATCH_DIR == "/tmp/work"
    assert "find . root mnt/session/outputs tmp/work " in prompt, (
        "a working file the agent put in /tmp/work has to travel with the rest"
    )
    assert "-path 'tmp/work/.*'" in prompt, (
        "only the non-hidden entries travel, exactly as under $HOME"
    )


# Names that the shell or tar would read as something else: separators, globs that
# match a system directory or everything, and a tar option that follows symlinks.
_HOSTILE_NAMES = ("my notes.txt", "new\nline.txt", "[e]tc", "*", "--dereference")


@pytest.mark.skipif(sys.platform != "linux", reason="the sandbox shell runs GNU find and tar")
def test_a_file_written_in_the_shell_working_directory_travels_and_restores(
    tmp_path: Path,
) -> None:
    """The sandbox shell starts in /, so `echo … > qa_keep.txt` "in your working
    directory" writes /qa_keep.txt. Runs the archive step with real bash, find
    and tar against a fake /, unpacks it with the command the successor is
    given, and applies the restore rule to a fake successor /."""
    old = tmp_path / "old"
    uploads = old / "mnt" / "session" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "credentials").write_text("SECRET\n")
    (old / "root").mkdir()
    (old / "root" / "HANDOFF.md").write_text("- Files: /qa_keep.txt\n")
    (old / "root" / "creds").symlink_to(uploads / "credentials")
    (old / "mnt" / "session" / "outputs").mkdir()
    (old / "etc").mkdir()
    (old / "etc" / "system-config").write_text("old system\n")
    (old / ".dockerenv").write_text("")
    (old / "qa_keep.txt").write_text("8e6287d4\n")
    for name in _HOSTILE_NAMES:
        (old / name).write_text(f"task file {name}\n")

    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    archive_step = prompt[prompt.index("  cd /\n") : prompt.index("members lists what travels")]
    # Every absolute path in the step, re-rooted at the fake /.
    script = re.sub(r"(?<=[\s=(])/", f"{old}/", archive_step).replace("\\\n", " ")
    subprocess.run(["bash", "-euc", script], check=True, capture_output=True)

    bundle = old / "mnt" / "session" / "outputs" / handoff_filename(TRANSFER_ID)
    handoff = tmp_path / "new" / "root" / "handoff"
    handoff.mkdir(parents=True)
    subprocess.run(["tar", "xzf", str(bundle), "-C", str(handoff)], check=True)
    assert not (handoff / "etc").exists(), "a name that globs to /etc must not carry /etc"
    assert not (handoff / ".dockerenv").exists(), "dotfiles in / stay behind"
    assert (handoff / "root" / "creds").is_symlink(), (
        "a symlink travels as a link, never as the excluded file it points at"
    )
    assert "SECRET" not in bundle.read_bytes().decode("latin-1")

    framing = render_handoff_framing(
        model_id="claude-sonnet-5",
        transfer_kind="full",
        bundle_mount_path=CHECKPOINT_BUNDLE_MOUNT_PATH,
        from_agent_name="a",
        to_agent_name="a",
        requested_work=None,
        previous_session=None,
        not_carried=(),
    )
    assert framing.system is not None
    assert "`handoff/<path>` goes back to `/<path>`" in framing.system.blocks[0]["text"]
    new = tmp_path / "new"
    (new / "etc").mkdir()
    (new / "etc" / "system-config").write_text("new system\n")
    for path in handoff.rglob("*"):
        if path.is_file() and not path.is_symlink():
            target = new / path.relative_to(handoff)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())

    assert (new / "qa_keep.txt").read_text() == "8e6287d4\n", "the file is back at /qa_keep.txt"
    for name in _HOSTILE_NAMES:
        assert (new / name).read_text() == f"task file {name}\n", f"{name!r} travels as itself"
    assert (new / "etc" / "system-config").read_text() == "new system\n", (
        "and restoring it touches no system path"
    )


def _run_archive_step(prompt: str, root: Path) -> str:
    """Run the prompt's archive step with real bash, find and tar against a fake /."""
    archive_step = prompt[prompt.index("  cd /\n") : prompt.index("members lists what travels")]
    # Every absolute path in the step, re-rooted at the fake /.
    script = re.sub(r"(?<=[\s=(])/", f"{root}/", archive_step).replace("\\\n", " ")
    done = subprocess.run(["bash", "-euc", script], check=True, capture_output=True, text=True)
    return done.stdout


def _noise(path: Path, size: int) -> None:
    """Incompressible bytes, so the archive is as large as the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(random.Random(size).randbytes(size))


def _fake_root(tmp_path: Path) -> Path:
    old = tmp_path / "old"
    (old / "mnt" / "session" / "outputs").mkdir(parents=True)
    (old / "tmp").mkdir()
    (old / "root" / "work").mkdir(parents=True)
    (old / "root" / "HANDOFF.md").write_text("- Files: /root/work\n")
    for n in range(20):
        (old / "root" / "work" / f"table{n}.csv").write_text("a,b\n" * 50)
    return old


def _members(bundle: Path) -> set[str]:
    listed = subprocess.run(["tar", "tzf", str(bundle)], check=True, capture_output=True)
    return set(listed.stdout.decode().splitlines())


@pytest.mark.skipif(sys.platform != "linux", reason="the sandbox shell runs GNU find and tar")
def test_an_over_cap_archive_is_rebuilt_without_its_largest_files(tmp_path: Path) -> None:
    """A workspace over the cap used to lose every file. The rebuild drops the
    largest files until the rest fits, so the note and the small outputs travel,
    and names each dropped file; a name tar would read as a pattern is dropped
    as itself, and one with a newline, which tar cannot be told, is kept."""
    old = _fake_root(tmp_path)
    _noise(old / "[big] *.bin", 600_000)
    _noise(old / "root" / "work" / "model.pkl", 500_000)
    _noise(old / "root" / "work" / "fit.nc", 300_000)
    _noise(old / "root" / "work" / "odd\nname.bin", 700_000)
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=1
    )

    output = _run_archive_step(prompt, old)

    paths, more = checkpoint_left_out(output)
    assert paths == (
        f"{old}/[big] *.bin",
        f"{old}/root/work/model.pkl",
        f"{old}/root/work/fit.nc",
    ), "largest first, and only as many as the budget needs"
    assert more == 0, "every left-out file is listed by name"
    bundle = old / "mnt" / "session" / "outputs" / handoff_filename(TRANSFER_ID)
    assert bundle.stat().st_size <= 1024 * 1024, "the rebuilt archive fits the cap"
    members = _members(bundle)
    assert {"root/HANDOFF.md", "root/work/table0.csv", "root/work/table19.csv"} <= members, (
        "the note and the small working files always travel"
    )
    # `tar t` prints a newline in a name as `\n`.
    assert "root/work/odd\\nname.bin" in members, "a name tar cannot exclude stays in"
    assert not {"./[big] *.bin", "root/work/model.pkl", "root/work/fit.nc"} & members, (
        "every listed file is really gone from the archive"
    )
    assert HANDOFF_TOO_LARGE_MARKER not in output, "a partial archive is not a rejection"


@pytest.mark.skipif(sys.platform != "linux", reason="the sandbox shell runs GNU find and tar")
def test_an_archive_still_over_the_cap_after_the_rebuild_is_deleted(tmp_path: Path) -> None:
    """Files the rebuild cannot leave out keep it over the cap: the legacy
    rejection still applies, and nothing is left in the outputs directory."""
    old = _fake_root(tmp_path)
    _noise(old / "root" / "work" / "a\n.bin", 700_000)
    _noise(old / "root" / "work" / "b\n.bin", 700_000)
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=1
    )

    output = _run_archive_step(prompt, old)

    assert checkpoint_too_large_bytes(output) is not None, "the rejection is reported"
    assert list((old / "mnt" / "session" / "outputs").iterdir()) == [], (
        "the over-cap archive is deleted, partial name and all"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="the sandbox shell runs GNU find and tar")
def test_an_archive_under_the_cap_is_built_once_and_leaves_nothing_out(tmp_path: Path) -> None:
    old = _fake_root(tmp_path)
    _noise(old / "root" / "work" / "model.pkl", 300_000)
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=1
    )

    output = _run_archive_step(prompt, old)

    assert checkpoint_left_out(output) == ((), 0), "nothing is left out under the cap"
    bundle = old / "mnt" / "session" / "outputs" / handoff_filename(TRANSFER_ID)
    assert "root/work/model.pkl" in _members(bundle), "every file travels"


def test_checkpoint_left_out_reads_whole_lines_and_the_overflow_count() -> None:
    reply = (
        "Archive rebuilt.\n"
        f"{HANDOFF_LEFT_OUT_MARKER} /root/work/model.pkl\n"
        f"  {HANDOFF_LEFT_OUT_MARKER} /my notes, v2.txt  \n"
        f"{HANDOFF_LEFT_OUT_MARKER} /root/work/model.pkl\n"
        f"{HANDOFF_LEFT_OUT_MORE_MARKER} 12\n"
        "-rw-r--r-- 1 root root 19000000 bundle.tar.gz\n"
    )
    assert checkpoint_left_out(reply) == (
        ("/root/work/model.pkl", "/my notes, v2.txt"),
        12,
    ), "each path once, in order, plus the count of the ones not named"


def test_checkpoint_left_out_ignores_an_echo_of_the_command() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert checkpoint_left_out(prompt) == ((), 0), (
        "quoting back the commands is not evidence that anything was left out"
    )

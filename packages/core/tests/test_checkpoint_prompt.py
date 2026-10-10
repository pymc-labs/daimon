"""Tests for the checkpoint turn's prompt and the readers for its reply."""

from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import daimon.core.checkpoint_prompt as checkpoint_module
import pytest
from daimon.core.checkpoint_prompt import (
    CHECKPOINT_BUNDLE_MOUNT_PATH,
    CHECKPOINT_EXCLUDED_GLOBS,
    CHECKPOINT_EXCLUDED_PATHS,
    CHECKPOINT_OUTPUTS_DIR,
    CHECKPOINT_SCRATCH_DIR,
    HANDOFF_FILENAME_PREFIX,
    HANDOFF_MAX_BYTES,
    HANDOFF_TOO_LARGE_MARKER,
    REPO_STATE_DIR,
    build_checkpoint_prompt,
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


def test_prompt_does_not_silently_omit_oversized_task_files() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=7
    )
    assert "--exclude-from=" not in prompt
    assert "Do not omit oversized task files" in prompt
    assert "HANDOFF_INCOMPLETE" in prompt


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
        f"git -C {REPO} diff --binary HEAD > /root/uncommitted.patch",
        f"git -C {REPO} ls-files --others --exclude-standard > /root/untracked.txt",
    ):
        assert command in with_repo, f"{command} is part of the repo-state step"
    assert with_repo.count(f"\n  git -C {REPO} rev-parse HEAD") == 2, (
        "HEAD is echoed before and after the archive; the saved HEAD is redirected"
    )
    assert "-C / root mnt/session/outputs tmp/work && touch" in with_repo, (
        "the repo mount is never a tar root: MA mounts it again"
    )
    assert "--exclude='mnt/repo/analytics'" in with_repo, "and it is excluded wherever it is"

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
    assert "-C / home/claude mnt/session/outputs tmp/work" in prompt, (
        "the tar roots are the given home directory, the outputs directory and /tmp, relative to /"
    )
    assert "/home/claude/repo-state/files.tar" in prompt, "repo state goes under that home"
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
    assert len(first.split()) < 900, "a long checkpoint prompt costs tokens on a billed turn"


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
        assert f"git -C {REPO} diff --binary HEAD > /root/uncommitted.patch" in prompt, (
            f"unsaved_work={unsaved_work!r} means capture the work, so the patch step stays"
        )
        assert f"git -C {REPO} ls-files --others --exclude-standard" in prompt, (
            "untracked files are part of the work being captured"
        )
        assert "/root/repo-state/local-commits.bundle" in prompt, "local commits travel too"
        assert "/root/repo-state/files.tar" in prompt, "and untracked and ignored files"


def test_prompt_leaves_uncommitted_changes_behind_when_the_answer_is_leave() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20, unsaved_work="leave"
    )
    assert "diff HEAD" not in prompt, (
        "the person chose to leave the changes, so nothing captures them as a patch"
    )
    assert "ls-files --others" not in prompt, "nor lists the untracked files to carry"
    assert "-C / root mnt/session/outputs tmp/work && touch" in prompt, (
        "the checkout must not be tarred either, or the changes would come across anyway"
    )
    assert "-C / root mnt/session/outputs mnt/repo" not in prompt, "the repo is not a root here"
    assert "--exclude-from=" not in prompt, "oversized files must not silently disappear"
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
        "outputs directory, and the unsaved work of the working repository if one is mounted."
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
            "    --exclude='root/.*' \\",
            "    --exclude='tmp/work/.*' \\",
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
            "    --exclude='mnt/repo/analytics' \\",
            "    -C / root mnt/session/outputs tmp/work && touch /tmp/daimon-handoff-built",
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
        f'size=$(stat -c %s {archive}); if [ "$size" -gt 20971520 ]; '
        f'then rm -f {archive}; echo "HANDOFF_TOO_LARGE $size"; '
        f"else ls -l {archive}; fi; fi" in prompt
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
    assert "--exclude-from=" not in prompt, "the size guard must reject incomplete transfers"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_git_step_captures_everything_a_fresh_mount_cannot_give_back(
    tmp_path: Path,
) -> None:
    """Review of #628, P1 2: with the mounted checkout no longer archived, its
    work must still be restorable -- a modified tracked BINARY, a local commit
    that is on no remote, untracked files and ignored task files. Runs the
    prompt's own commands against a real repository, then restores into a
    fresh clone the way the successor's framing says to."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "model.bin").write_bytes(bytes(range(256)))
    (seed / ".gitignore").write_text("generated.csv\nnode_modules/\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "origin", "HEAD:main")

    repo = tmp_path / "mnt" / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    (repo / "notes.md").write_text("local commit\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-q", "-m", "local only")
    (repo / "model.bin").write_bytes(bytes(reversed(range(256))))
    (repo / "data").mkdir()
    (repo / "data" / "new.py").write_text("print('untracked')\n")
    (repo / "generated.csv").write_text("a,b\n1,2\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text("reproducible\n")

    home = tmp_path / "home"
    home.mkdir()
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID,
        repo_mount_path=str(repo),
        max_bundle_mib=20,
        home_dir=str(home),
    )
    step = prompt.split("record the repository state.", 1)[1].split("NEVER RUN GIT", 1)[0]
    commands = [line.strip() for line in step.splitlines() if line.startswith("  ")]
    subprocess.run(["bash", "-c", "\n".join(commands)], check=True, cwd=tmp_path)
    assert f"--exclude='{str(repo).strip('/')}'" in prompt, "the checkout itself never travels"

    state = home / REPO_STATE_DIR
    successor = tmp_path / "successor"
    _git(tmp_path, "clone", "-q", str(origin), str(successor))  # a fresh mount
    assert (state / "remote.txt").read_text().strip() == str(origin)
    _git(successor, "fetch", "-q", str(state / "local-commits.bundle"), "HEAD")
    _git(successor, "merge", "-q", "--ff-only", "FETCH_HEAD")
    assert _git(successor, "rev-parse", "HEAD") == (state / "head.txt").read_text()
    _git(successor, "apply", "--binary", str(home / "uncommitted.patch"))
    subprocess.run(["tar", "xf", str(state / "files.tar")], cwd=successor, check=True)

    assert (successor / "model.bin").read_bytes() == bytes(reversed(range(256)))
    assert (successor / "notes.md").read_text() == "local commit\n"
    assert (successor / "data" / "new.py").read_text() == "print('untracked')\n"
    assert (successor / "generated.csv").read_text() == "a,b\n1,2\n", "ignored task files too"
    assert not (successor / "node_modules").exists(), "reproducible dirs stay behind"


def test_other_checkouts_under_the_archived_roots_still_travel() -> None:
    """Review of #628, P1 1: only the MOUNTED checkout is left out (MA mounts it
    again). A repository the agent cloned under $HOME keeps its working files in
    the archive, transfer after transfer, exactly as before."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    tar = prompt[prompt.index("tar czf") :]
    assert "-name .git" not in prompt, "no blanket exclusion of nested checkouts"
    excluded = re.findall(r"--exclude='([^']+)'", tar)
    assert [path for path in excluded if path.startswith("root/")] == ["root/.*"], (
        "nothing under the home directory is excluded except its dot entries and caches"
    )
    assert "mnt/repo/analytics" in excluded, "the mounted checkout alone is left out"


def _execute_checkpoint(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, *, repo: Path | None = None, cap: int = 20
) -> tuple[Path, str]:
    home = sandbox / "root"
    outputs = sandbox / "mnt/session/outputs"
    uploads = sandbox / "mnt/session/uploads"
    scratch = sandbox / "tmp/work"
    for path in (home, outputs, uploads, scratch):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(checkpoint_module, "CHECKPOINT_OUTPUTS_DIR", str(outputs))
    monkeypatch.setattr(checkpoint_module, "CHECKPOINT_SCRATCH_DIR", str(scratch))
    monkeypatch.setattr(
        checkpoint_module, "CHECKPOINT_BUNDLE_MOUNT_PATH", str(uploads / "daimon-handoff.tar.gz")
    )
    monkeypatch.setattr(checkpoint_module, "_BUILT_MARKER_PATH", str(sandbox / "built"))
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID,
        repo_mount_path=str(repo) if repo else None,
        home_dir=str(home),
        max_bundle_mib=cap,
    )
    (home / "HANDOFF.md").write_text("Task: preserve the files\n")
    commands = "\n".join(line.strip() for line in prompt.splitlines() if line.startswith("  "))
    result = subprocess.run(["bash", "-c", commands], capture_output=True, text=True)
    assert result.returncode == 0 or "HANDOFF_INCOMPLETE" in result.stdout, result.stderr
    return outputs / handoff_filename(TRANSFER_ID), result.stdout


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")


def test_nested_checkout_work_survives_two_real_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    clone = first / "root/work/clone"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    (clone / "tracked.bin").write_bytes(b"initial\0")
    _git(clone, "add", ".")
    _git(clone, "commit", "-qm", "initial")
    expected = b"modified binary\0"
    (clone / "tracked.bin").write_bytes(expected)
    (clone / "notes.md").write_text("untracked work")
    archive, _ = _execute_checkpoint(first, monkeypatch)
    restored = tmp_path / "restored"
    _extract(archive, restored)
    second = tmp_path / "second"
    (second / "root").mkdir(parents=True)
    shutil.copytree(restored / str(clone).lstrip("/"), second / "root/work/clone")
    archive, _ = _execute_checkpoint(second, monkeypatch)
    twice = tmp_path / "twice"
    _extract(archive, twice)
    clone = twice / str(second / "root/work/clone").lstrip("/")
    assert (clone / "tracked.bin").read_bytes() == expected
    assert (clone / "notes.md").read_text() == "untracked work"


def test_unpacked_inherited_archive_is_not_required_for_the_next_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    (first / "root/work").mkdir(parents=True)
    (first / "root/work/data.csv").write_text("irreplaceable task data")
    archive, _ = _execute_checkpoint(first, monkeypatch)
    second = tmp_path / "second"
    uploads = second / "mnt/session/uploads"
    uploads.mkdir(parents=True)
    shutil.copyfile(archive, uploads / "daimon-handoff.tar.gz")
    archive, _ = _execute_checkpoint(second, monkeypatch)
    restored = tmp_path / "restored"
    _extract(archive, restored)
    inherited = restored / str(second / "root/inherited-handoff.tar.gz").lstrip("/")
    original = tmp_path / "original"
    _extract(inherited, original)
    assert (original / str(first / "root/work/data.csv").lstrip("/")).read_text() == (
        "irreplaceable task data"
    )


def test_oversized_task_file_degrades_instead_of_reporting_a_partial_archive_as_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import random

    home = tmp_path / "root"
    home.mkdir()
    (home / "task.bin").write_bytes(random.Random(0).randbytes(2 * 1024 * 1024))
    archive, reply = _execute_checkpoint(tmp_path, monkeypatch, cap=1)
    assert "HANDOFF_TOO_LARGE" in reply
    assert not archive.exists()


def test_failed_repo_capture_cannot_report_a_full_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    # No HEAD: diff/bundle restoration cannot succeed. The shell still builds
    # an archive, but the missing capture sentinel must reject it.
    archive, reply = _execute_checkpoint(tmp_path, monkeypatch, repo=repo)
    assert "HANDOFF_INCOMPLETE" in reply
    assert not archive.exists()


def test_mounted_repo_work_survives_a_repo_switch_and_two_transfers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "asset.bin").write_bytes(b"initial\0")
    (seed / ".gitignore").write_text("generated.csv\n*.env\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "initial")
    _git(seed, "push", "-q", "origin", "main")
    old_repo = tmp_path / "old-repo"
    _git(tmp_path, "clone", "-q", str(origin), str(old_repo))
    (old_repo / "local.md").write_text("unpublished commit")
    _git(old_repo, "add", ".")
    _git(old_repo, "commit", "-qm", "local")
    saved_head = _git(old_repo, "rev-parse", "HEAD").strip()
    fork = tmp_path / "fork.git"
    _git(tmp_path, "init", "-q", "--bare", str(fork))
    _git(old_repo, "remote", "add", "fork", str(fork))
    _git(old_repo, "push", "-q", "fork", "HEAD:main")
    # Published to another remote is still unavailable from the saved origin.
    (old_repo / "asset.bin").write_bytes(b"modified\0binary")
    (old_repo / "generated.csv").write_text("ignored task data")
    (old_repo / "notes with spaces.md").write_text("untracked task work")
    (old_repo / "private.env").write_text("excluded credential fixture")
    destination_repo = tmp_path / "destination-repo"
    destination_repo.mkdir()
    (destination_repo / "keep.md").write_text("destination task")

    # The new agent has a DIFFERENT repo, so restore the old remote separately.
    for hop in range(2):
        sandbox = tmp_path / f"sandbox-{hop}"
        archive, reply = _execute_checkpoint(sandbox, monkeypatch, repo=old_repo)
        assert "HANDOFF_INCOMPLETE" not in reply
        restored = tmp_path / f"restored-{hop}"
        _extract(archive, restored)
        home = restored / str(sandbox / "root").lstrip("/")
        state = home / REPO_STATE_DIR
        repo = tmp_path / f"separate-checkout-{hop}"
        _git(tmp_path, "clone", "-q", (state / "remote.txt").read_text().strip(), str(repo))
        _git(repo, "fetch", "-q", str(state / "local-commits.bundle"), "HEAD")
        _git(repo, "checkout", "-q", saved_head)
        _git(repo, "apply", "--binary", str(home / "uncommitted.patch"))
        subprocess.run(["tar", "xf", str(state / "files.tar"), "-C", str(repo)], check=True)
        assert (repo / "asset.bin").read_bytes() == b"modified\0binary"
        assert (repo / "generated.csv").read_text() == "ignored task data"
        assert (repo / "local.md").read_text() == "unpublished commit"
        assert (repo / "notes with spaces.md").read_text() == "untracked task work"
        assert not (repo / "private.env").exists()
        assert _git(repo, "rev-parse", "HEAD").strip() == saved_head
        old_repo = repo
    assert (destination_repo / "keep.md").read_text() == "destination task"


def test_ignored_repo_files_are_bounded_without_silent_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("generated.csv\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    (repo / "generated.csv").write_bytes(b"x" * (2 * 1024 * 1024))
    archive, reply = _execute_checkpoint(tmp_path, monkeypatch, repo=repo, cap=1)
    assert "HANDOFF_INCOMPLETE" in reply
    assert not archive.exists()


def test_repo_without_a_remote_carries_a_self_contained_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "task.md").write_text("local task")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    archive, reply = _execute_checkpoint(tmp_path, monkeypatch, repo=repo)
    assert "HANDOFF_INCOMPLETE" not in reply
    restored = tmp_path / "restored"
    _extract(archive, restored)
    bundle = restored / str(tmp_path / "root/repo-state/local-commits.bundle").lstrip("/")
    successor = tmp_path / "successor"
    _git(tmp_path, "clone", "-q", str(bundle), str(successor))
    assert (successor / "task.md").read_text() == "local task"

"""Tests for the checkpoint turn's prompt and the readers for its reply."""

from __future__ import annotations

import random
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
    checkpoint_omitted_files,
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
    assert f"/mnt/session/outputs/{filename}" in prompt, (
        "the archive must be written flat into the outputs directory"
    )
    assert f"ls -l /mnt/session/outputs/{filename}" in prompt, (
        "the reply has to show the archive so the transfer can read the size back"
    )


def test_archive_excludes_credentials_caches_and_only_the_mounted_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "root"
    (home / "work/nested").mkdir(parents=True)
    (home / "work/nested/.git/objects").mkdir(parents=True)
    (home / "work/nested/.git/objects/blob").write_text("cache")
    (home / "work/nested/task.md").write_text("keep nested repo work")
    (home / "work/nested/private.env").write_text("excluded credential fixture")
    (home / ".ssh").mkdir()
    (home / ".ssh/key").write_text("excluded credential fixture")
    for name in CHECKPOINT_EXCLUDED_GLOBS:
        directory = home / "work" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cache").write_bytes(b"x" * (2 * 1024 * 1024))
    archive, reply = _execute_checkpoint(tmp_path, monkeypatch, cap=1)
    assert "HANDOFF_OMITTED" not in reply
    with tarfile.open(archive) as bundle:
        names = bundle.getnames()
        assert str(home / "work/nested/task.md").lstrip("/") in names
        assert not any(
            "cache" in n or ".ssh" in n or n.endswith(".env") or "/objects/" in n for n in names
        )


def test_prompt_does_not_silently_omit_oversized_task_files() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=7
    )
    assert "--exclude-from=" not in prompt
    assert "HANDOFF_OMITTED" in prompt
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
    assert with_repo.count(f"\n  git -C {REPO} rev-parse HEAD\n") == 2, (
        "HEAD is echoed before and after the archive; the saved HEAD is redirected"
    )
    assert "roots = [pathlib.Path(p) for p in (home, outputs, scratch)]" in with_repo
    assert "str(path).startswith(repo + '/')" in with_repo

    without_repo = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert "git -C" not in without_repo, "no repo means no git commands"
    assert "NEVER RUN GIT" not in without_repo, "and no repo prohibition to state"
    assert "python3 - /root /mnt/session/outputs /tmp/work" in without_repo, (
        "the home directory, the outputs directory and /tmp are archived either way"
    )


def test_prompt_numbers_steps_consecutively_when_the_repo_step_is_skipped() -> None:
    without_repo = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    headers = re.findall(r"^Step (\d+) - ", without_repo, re.MULTILINE)
    assert headers == ["1", "2", "3", "4", "5"], "steps are renumbered, not left with a hole"


def test_prompt_honours_a_non_default_home_dir() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID,
        repo_mount_path=REPO,
        max_bundle_mib=20,
        home_dir="/home/claude",
    )
    assert "/home/claude/HANDOFF.md" in prompt, "the note goes in the given home directory"
    assert "/home/claude/uncommitted.patch" in prompt, "so does the patch"
    assert "python3 - /home/claude /mnt/session/outputs /tmp/work" in prompt
    assert "/home/claude/repo-state/files.tar" in prompt, "repo state goes under that home"
    assert "relative.parts[0].startswith('.')" in prompt
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
    assert len(first.split()) < 1600, "a long checkpoint prompt costs tokens on a billed turn"


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
    assert "roots = [pathlib.Path(p) for p in (home, outputs, scratch)]" in prompt, (
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
    assert "python3 - /root /mnt/session/outputs /tmp/work" in prompt
    assert "path.name.startswith('daimon-handoff-')" in prompt


def test_prompt_excludes_every_dot_entry_directly_under_home() -> None:
    """Issue 1b: the base image ships ~100 MB of toolchain caches in $HOME
    before the task writes anything, so the bundle blew the cap on an empty
    task. One pattern covers .ssh and every one of them."""
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=None, max_bundle_mib=20
    )
    assert "relative.parts[0].startswith('.')" in prompt, (
        "every dot entry directly under $HOME - .ssh, .bun, .cargo, .rustup, .gradle, "
        ".npm, .local, .config - is excluded with its subtree"
    )


def test_capture_commands_use_readable_heredocs() -> None:
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    assert "bash -e -o pipefail <<'DAIMON_REPO'" in prompt
    assert "<<'DAIMON_FILES'" in prompt
    assert "<<'DAIMON_ARCHIVE'" in prompt
    assert "bash -e -o pipefail -c" not in prompt


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
    assert "python3 - /root /mnt/session/outputs /tmp/work" in prompt, (
        "a working file the agent put in /tmp/work has to travel with the rest"
    )
    assert "relative.parts[0].startswith('.')" in prompt, (
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
    commands = [line[2:] for line in step.splitlines() if line.startswith("  ")]
    subprocess.run(["bash", "-c", "\n".join(commands)], check=True, cwd=tmp_path)
    assert str(repo) in prompt

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
    prompt = build_checkpoint_prompt(
        transfer_id=TRANSFER_ID, repo_mount_path=REPO, max_bundle_mib=20
    )
    assert "-name .git" not in prompt
    assert "str(path).startswith(repo + '/')" in prompt


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
    commands = "\n".join(line[2:] for line in prompt.splitlines() if line.startswith("  "))
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


@pytest.mark.parametrize("restore", [True, False])
def test_five_hops_preserve_five_mib_without_nesting_or_size_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore: bool
) -> None:
    sandbox = tmp_path / "session"
    work = sandbox / "root/work/data.bin"
    work.parent.mkdir(parents=True)
    expected = random.Random(628).randbytes(5 * 1024 * 1024)
    work.write_bytes(expected)
    sizes = []
    for hop in range(5):
        archive, reply = _execute_checkpoint(sandbox, monkeypatch)
        assert "HANDOFF_TOO_LARGE" not in reply and "HANDOFF_INCOMPLETE" not in reply
        sizes.append(archive.stat().st_size)
        with tarfile.open(archive) as bundle:
            assert not any("inherited-handoff.tar.gz" in m.name for m in bundle)
            assert bundle.extractfile(str(work).lstrip("/")).read() == expected
        saved = tmp_path / f"bundle-{hop}.tar.gz"
        shutil.copyfile(archive, saved)
        shutil.rmtree(sandbox)
        uploads = sandbox / "mnt/session/uploads"
        uploads.mkdir(parents=True)
        shutil.copyfile(saved, uploads / "daimon-handoff.tar.gz")
        if restore:
            restored = tmp_path / f"restored-{hop}"
            _extract(saved, restored)
            shutil.copytree(restored / str(sandbox / "root").lstrip("/"), sandbox / "root")
    assert all(5 * 1024 * 1024 <= size < 5.1 * 1024 * 1024 for size in sizes), sizes
    assert max(sizes) - min(sizes) < 4096, sizes
    print(f"five-hop sizes ({restore=}): {sizes}")


def test_oversized_file_is_named_and_small_notes_still_travel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "root"
    home.mkdir()
    oversized = home / "large file.bin"
    oversized.write_bytes(random.Random(0).randbytes(2 * 1024 * 1024))
    (home / "notes.md").write_text("small notes must survive")
    archive, reply = _execute_checkpoint(tmp_path, monkeypatch, cap=1)
    assert checkpoint_omitted_files(reply) == (str(oversized),)
    with tarfile.open(archive) as bundle:
        assert (
            bundle.extractfile(str(home / "notes.md").lstrip("/")).read()
            == b"small notes must survive"
        )
        assert str(oversized).lstrip("/") not in bundle.getnames()
        assert (
            str(oversized).encode()
            in bundle.extractfile(str(home / "HANDOFF.md").lstrip("/")).read()
        )


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


def test_ignored_repo_size_omissions_preserve_other_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("generated.csv\n.mypy_cache/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    (repo / "generated.csv").write_bytes(b"x" * (2 * 1024 * 1024))
    (repo / ".mypy_cache").mkdir()
    (repo / ".mypy_cache/cache").write_bytes(b"x" * (25 * 1024 * 1024))
    (repo / "notes.md").write_text("small untracked work")
    archive, reply = _execute_checkpoint(tmp_path, monkeypatch, repo=repo, cap=1)
    assert checkpoint_omitted_files(reply) == (str(repo / "generated.csv"),)
    restored = tmp_path / "restored"
    _extract(archive, restored)
    files = restored / str(tmp_path / "root/repo-state/files.tar").lstrip("/")
    with tarfile.open(files) as bundle:
        assert bundle.extractfile("notes.md").read() == b"small untracked work"
        assert "generated.csv" not in bundle.getnames()


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


@pytest.mark.parametrize("restore", [False, True])
def test_mounted_repo_five_hops_keep_one_payload_even_if_never_unpacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore: bool
) -> None:
    origin, seed = tmp_path / "origin.git", tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / ".gitignore").write_text("data.bin\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "initial")
    _git(seed, "push", "-q", "origin", "HEAD:main")
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    expected = random.Random(628).randbytes(5 * 1024 * 1024)
    (repo / "data.bin").write_bytes(expected)
    sandbox = tmp_path / ("long-session-" + "x" * 100)
    sizes = []
    for hop in range(5):
        if hop and restore:
            # Only timestamps change; this must not create historical payload copies.
            import os

            os.utime(repo / "data.bin", (hop, hop))
        archive, reply = _execute_checkpoint(sandbox, monkeypatch, repo=repo)
        assert "HANDOFF_TOO_LARGE" not in reply and "HANDOFF_INCOMPLETE" not in reply
        sizes.append(archive.stat().st_size)
        restored = tmp_path / f"restored-repo-{hop}"
        _extract(archive, restored)
        home = restored / str(sandbox / "root").lstrip("/")
        artifacts = list(home.rglob("files.tar"))
        payloads = []
        for artifact in artifacts:
            with tarfile.open(artifact) as files:
                if "data.bin" in files.getnames():
                    payloads.append(files.extractfile("data.bin").read())
        assert payloads == [expected], "one current/restorable payload, including skip path"
        saved = tmp_path / f"repo-bundle-{hop}.tar.gz"
        shutil.copyfile(archive, saved)
        shutil.rmtree(sandbox)
        uploads = sandbox / "mnt/session/uploads"
        uploads.mkdir(parents=True)
        shutil.copyfile(saved, uploads / "daimon-handoff.tar.gz")
        if not restore:
            (repo / "data.bin").unlink(missing_ok=True)
    assert all(5 * 1024 * 1024 <= size < 5.1 * 1024 * 1024 for size in sizes), sizes
    assert max(sizes) - min(sizes) < 8192, sizes
    print(f"mounted five-hop sizes ({restore=}): {sizes}")


def test_corrupt_inherited_archive_removes_any_stale_output_and_reports_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads = tmp_path / "mnt/session/uploads"
    uploads.mkdir(parents=True)
    (uploads / "daimon-handoff.tar.gz").write_bytes(b"invalid gzip")
    outputs = tmp_path / "mnt/session/outputs"
    outputs.mkdir(parents=True)
    (outputs / handoff_filename(TRANSFER_ID)).write_bytes(b"stale output must not transfer")
    archive, reply = _execute_checkpoint(tmp_path, monkeypatch)
    assert "HANDOFF_INCOMPLETE" in reply
    assert not archive.exists()


def test_omission_parser_accepts_indented_output_but_never_promotes_malformed_omissions() -> None:
    assert checkpoint_omitted_files('  HANDOFF_OMITTED "/root/large file.bin"  ') == (
        "/root/large file.bin",
    )
    assert checkpoint_omitted_files("HANDOFF_OMITTED bad-json")
    assert checkpoint_omitted_files("HANDOFF_OMITTED []")
    assert not checkpoint_omitted_files("print('HANDOFF_OMITTED ' + json.dumps(name))")

"""The checkpoint turn's prompt, and the readers for its reply.

When a task's workspace has to be replaced, daimon spends one bounded,
billed turn on the OLD session asking it to write down what it was doing and
tar its own files into the session's outputs directory. That archive is the
only way working files cross into the successor: the Files API lists
``/mnt/session/outputs/*`` and mounted uploads and nothing else, so the
working directory, ``/tmp/work`` and the repo checkout are invisible to daimon
unless the session itself packs them (capability matrix P4.b).

Everything here is pure string work. The module deliberately imports nothing
from the rest of ``daimon.core``: it is the one piece of the transfer path
that has to be readable on its own, because a wrong word in the prompt costs
a real turn and can lose real work.

Sandbox facts the prompt is built around (all observed live, capability
matrix §A/§D):

- A bash turn starts with cwd ``/`` and ``HOME=/root``, so every path in the
  prompt is absolute or explicitly relative to ``/``.
- The outputs listing is basename-only, so the archive is written flat and
  recognised later by its name prefix (:func:`is_handoff_filename`).
- ``/mnt/session/uploads`` (credential and bundle mounts), ``/mnt/memory``
  (agent memory) and ``/mnt/skills`` are mounts belonging to the destination,
  never to the task; they must not travel in the bundle.
- The agent's file tool writes the task's own files to
  ``/mnt/session/outputs``, so that directory is an archived ROOT (minus the
  bundle itself), not an exclusion. ``$HOME`` ships a populated toolchain
  (``.bun``, ``.cargo``, ``.rustup``, ``.gradle``, ``.npm``, ``.local``,
  ``.config``) that is ~100 MB before the task writes anything, so every dot
  entry directly under ``$HOME`` is excluded and only the non-hidden ones are
  carried.
- ``/tmp/work`` is an archived root too, minus its own dot entries and this
  module's scratch file. It is not where daimon would choose to put working
  files, but it is where an agent asked to "create notes.md" reached for
  first (observed live), and a root nothing writes to costs nothing.
- The prompt is written to be legible as HOST instruction rather than chat
  text. It travels twice: in the user message, behind a ``<turn_controls>``
  element carrying a ``checkpoint`` block, and again on the
  ``system.message`` channel where the old session's model supports one.
  Models that refused it named a missing ``<turn_controls>`` as the tell, and
  named "reply with the output and nothing else" as what turned an odd
  request into an exfiltration-shaped one; neither is in it now.
"""

from __future__ import annotations

import re
import shlex
import uuid
from typing import Literal

HANDOFF_FILENAME_PREFIX = "daimon-handoff-"

# Daimon's own cap on a transfer bundle, equal to
# ``output_delivery.MAX_BYTES_PER_FILE`` by design: a bundle is downloaded
# through the same path as any other session output. Not an MA limit — 25 MiB
# uploads were accepted live (P4.g). Deliberately NOT imported from
# ``output_delivery``; a test asserts the two stay equal.
HANDOFF_MAX_BYTES = 20 * 1024 * 1024

# Absolute mount points that must never enter a bundle: they hold the
# destination's own credentials, the agent's memory store and the platform's
# skills. The outputs directory is deliberately NOT here — it is where the
# file tool writes the task's own work, so it is an archived root.
CHECKPOINT_EXCLUDED_PATHS: tuple[str, ...] = (
    "/mnt/session/uploads",
    "/mnt/memory",
    "/mnt/skills",
)

#: Where the file tool writes, where the archive is written, and the second
#: archived root.
CHECKPOINT_OUTPUTS_DIR = "/mnt/session/outputs"

#: Printed by the checkpoint turn instead of a listing when the finished
#: archive is over the cap. The archive is deleted first, so a rejected
#: transfer leaves nothing behind in the old session's outputs.
HANDOFF_TOO_LARGE_MARKER = "HANDOFF_TOO_LARGE"
HANDOFF_INCOMPLETE_MARKER = "HANDOFF_INCOMPLETE"

#: Where the host mounts the finished bundle in the SUCCESSOR workspace. The
#: prompt names it so the session it asks can see what the archive is for.
#: Deliberately NOT imported from ``workspace_transfer`` (this module imports
#: nothing from the rest of ``daimon.core``); a test asserts the two agree.
CHECKPOINT_BUNDLE_MOUNT_PATH = "/mnt/session/uploads/daimon-handoff.tar.gz"

# Directory names that are large, reproducible, or both. Matched at any depth.
CHECKPOINT_EXCLUDED_GLOBS: tuple[str, ...] = (
    ".git/objects",
    "node_modules",
    ".venv",
    "__pycache__",
    ".cache",
)

_BUILT_MARKER_PATH = "/tmp/daimon-handoff-built"

#: Where the git step saves what a fresh mount of the repository cannot give
#: back: its remote, HEAD and branch, the commits on no remote
#: (`local-commits.bundle`) and its untracked and ignored files
#: (`files.tar`). The binary patch stays at `$HOME/uncommitted.patch`.
REPO_STATE_DIR = "repo-state"

# A bounded, NUL-separated inventory, including ignored task files. Tar does
# not dereference symlinks. Fail the capture rather than silently skip bytes.
_REPO_FILE_LIST_SCRIPT = (
    "import os,subprocess,sys; "
    "paths=subprocess.check_output(['git','-C',sys.argv[1],'ls-files','-z','--others'])"
    ".split(b'\\0'); "
    "paths=[p for p in paths if p and not p.endswith(b'.env') and not "
    "{b'node_modules',b'.venv',b'__pycache__',b'.cache',b'.git'}.intersection(p.split(b'/'))]; "
    "total=sum(os.lstat(os.path.join(os.fsencode(sys.argv[1]),p)).st_size for p in paths); "
    "total<=int(sys.argv[2]) or sys.exit('HANDOFF_INCOMPLETE: repository files exceed cap'); "
    "sys.stdout.buffer.write(b'\\0'.join(paths)+(b'\\0' if paths else b''))"
)

#: The third archived root, after ``$HOME`` and the outputs directory. Only this
#: subdirectory of ``/tmp``: the base image ships ~60 MB of non-hidden browser
#: and compile-cache trees directly under ``/tmp`` (observed on staging), which
#: pushed an otherwise empty archive over the 20 MiB cap.
CHECKPOINT_SCRATCH_DIR = "/tmp/work"

_SHA1_LINE = re.compile(r"^\s*([0-9a-f]{40})\s*$", re.MULTILINE)

_TOO_LARGE_LINE = re.compile(rf"^\s*{HANDOFF_TOO_LARGE_MARKER}\s+(\d+)\s*$", re.MULTILINE)


def handoff_filename(transfer_id: uuid.UUID) -> str:
    """The flat outputs filename for one transfer's bundle."""

    return f"{HANDOFF_FILENAME_PREFIX}{transfer_id}.tar.gz"


def is_handoff_filename(filename: str) -> bool:
    """True for a transfer bundle's basename.

    Prefix-only by design: the outputs listing gives basenames, and the
    output sweep uses this to leave bundles in place instead of posting and
    deleting them.
    """

    return filename.startswith(HANDOFF_FILENAME_PREFIX)


def _relative_to_root(path: str) -> str:
    """``/root`` -> ``root``: a tar member prefix under ``-C /``."""

    return path.strip().strip("/")


def build_checkpoint_prompt(
    *,
    transfer_id: uuid.UUID,
    repo_mount_path: str | None,
    max_bundle_mib: int,
    home_dir: str = "/root",
    unsaved_work: Literal["copy", "leave"] | None = None,
) -> str:
    """The single user message sent to the old session's checkpoint turn.

    Deterministic: same inputs, same bytes. ``repo_mount_path`` adds the
    git-state step and the prohibition on changing the repository; without a
    repo neither appears.

    ``unsaved_work`` is what the person answered when asked about uncommitted
    changes in that repository, and only matters when one is mounted. The
    default (and ``"copy"``) captures everything a fresh mount of the
    repository cannot give back: a binary patch, the commits on no remote and
    the untracked and ignored files (`REPO_STATE_DIR`); the checkout itself is
    never archived. ``"leave"`` is the person saying those changes stay where
    they are, so the prompt does not capture them — the successor clones the repository fresh,
    and the copy that promises "the uncommitted changes stay in the old
    checkout" stays true.
    """

    archive_path = f"{CHECKPOINT_OUTPUTS_DIR}/{handoff_filename(transfer_id)}"
    max_bundle_bytes = max_bundle_mib * 1024 * 1024
    leave_unsaved = repo_mount_path is not None and unsaved_work == "leave"
    home_root = _relative_to_root(home_dir)
    outputs_root = _relative_to_root(CHECKPOINT_OUTPUTS_DIR)
    scratch_root = _relative_to_root(CHECKPOINT_SCRATCH_DIR)
    # The mounted repository is never archived: MA mounts it again for the
    # successor, and packing the checkout carried every tracked file (368 MB on
    # prod against the 20 MiB cap, minutes of compression, then every file
    # lost). What a fresh mount cannot give back is captured under $HOME by the
    # git step instead (`REPO_STATE_DIR`). Other git checkouts under the
    # archived roots are not touched: their working files travel as before.
    roots = [home_root, outputs_root, scratch_root]
    roots_argument = " ".join(roots)

    excludes: list[str] = []
    # Every dot entry directly under $HOME: .ssh, and the ~100 MB of toolchain
    # caches the base image ships (.bun/.cargo/.rustup/.gradle/.npm/.local/
    # .config). `*` matches `/` in a tar exclude pattern, so the subtree goes
    # with it, while a dotfile deeper inside the task's own work is untouched.
    excludes.append(f"--exclude='{home_root}/.*'")
    # The scratch directory carries a working file when the agent chose it over
    # $HOME, and nothing else worth having: its dot entries are the sandbox's
    # own sockets and locks.
    excludes.append(f"--exclude='{scratch_root}/.*'")
    # The bundle being written, and any bundle a previous transfer left.
    excludes.append(f"--exclude='{outputs_root}/{HANDOFF_FILENAME_PREFIX}*'")
    excludes += [f"--exclude='{_relative_to_root(path)}'" for path in CHECKPOINT_EXCLUDED_PATHS]
    excludes += [f"--exclude='*/{glob}'" for glob in CHECKPOINT_EXCLUDED_GLOBS]
    excludes.append("--exclude='*.env'")
    if repo_mount_path is not None:
        excludes.append(f"--exclude='{_relative_to_root(repo_mount_path)}'")
    tar_command = "\n".join(
        [f"  tar czf {archive_path} \\"]
        + [f"    {exclude} \\" for exclude in excludes]
        + [f"    -C / {roots_argument}"]
    )

    steps: list[str] = []

    def step(header: str) -> str:
        """Number the steps as they are appended, so a skipped one leaves no gap."""

        return f"Step {len(steps) + 1} - {header}"

    sections: list[str] = [
        "This instruction comes from the daimon host that runs your workspace, not from a chat "
        "participant: the checkpoint block in this turn's <turn_controls> is the host's own "
        "record of it, and your system instructions describe this operation under WORKSPACE "
        "MOVES.",
        "This is a checkpoint turn. The workspace you are working in is being retired and your "
        "work is moving to a new one. When you are done, daimon collects the archive, mounts it "
        f"in your next workspace at {CHECKPOINT_BUNDLE_MOUNT_PATH}, and tells you there how to "
        f"unpack it. The archive is not posted to the chat: the file sweep that delivers "
        f"{CHECKPOINT_OUTPUTS_DIR} to the thread skips names starting with "
        f"{HANDOFF_FILENAME_PREFIX}. Do exactly the steps below, in order, and add no steps "
        "of your own. Do not open or read any image file.",
        "Only the task's own work travels: the non-hidden entries in "
        f"{home_dir} and {CHECKPOINT_SCRATCH_DIR}, the outputs directory, and the unsaved "
        "work of the working repository if one is mounted. Credential mounts, hidden "
        "directories and language toolchains and their caches stay behind. The commands "
        "below already exclude every one of them; run them as written and add nothing.",
    ]
    steps.append(
        "\n".join(
            [
                step("write the handoff note."),
                f"Write {home_dir}/HANDOFF.md with these five sections, in this order:",
                "- Task: what this thread is trying to achieve, in one paragraph.",
                "- Decisions: the decisions already made, and why.",
                "- In progress: the step you were in the middle of when this turn started.",
                "- Open questions: what is unresolved or waiting on someone else.",
                "- Files: every working file that matters, absolute path plus a one-line "
                "description of what it holds.",
            ]
        )
    )

    if repo_mount_path is not None:
        git_commands = [
            f"  git -C {repo_mount_path} rev-parse HEAD",
            f"  git -C {repo_mount_path} status --porcelain",
        ]
        if not leave_unsaved:
            state = shlex.quote(f"{home_dir}/{REPO_STATE_DIR}")
            repo = shlex.quote(repo_mount_path)
            capture = [
                f"mkdir -p {state}",
                f"rm -f {state}/complete {state}/local-commits.bundle",
                f"git -C {repo} remote get-url origin > {state}/remote.txt || :",
                f"git -C {repo} rev-parse HEAD > {state}/head.txt",
                f"git -C {repo} rev-parse --abbrev-ref HEAD > {state}/branch.txt",
                f"git -C {repo} diff --binary HEAD > {shlex.quote(home_dir)}/uncommitted.patch",
                f"git -C {repo} ls-files --others --exclude-standard "
                f"> {shlex.quote(home_dir)}/untracked.txt",
                f"if [ -s {state}/remote.txt ]; then "
                f"git -C {repo} rev-list HEAD --not --remotes=origin > {state}/commits.txt; "
                f"else git -C {repo} rev-list HEAD > {state}/commits.txt; fi",
                f"if [ -s {state}/commits.txt ]; then git -C {repo} bundle create"
                f" {state}/local-commits.bundle HEAD "
                f"$(if [ -s {state}/remote.txt ]; then echo --not --remotes=origin; fi); fi",
                f"python3 -c {shlex.quote(_REPO_FILE_LIST_SCRIPT)} {repo} {max_bundle_bytes}"
                f" > {state}/files.list",
                f"tar -C {repo} --null -T {state}/files.list -cf {state}/files.tar",
                f"touch {state}/complete",
            ]
            git_commands.append(f"  bash -e -o pipefail -c {shlex.quote('; '.join(capture))}")
        disposition = (
            "The uncommitted changes in this checkout are deliberately being left behind: "
            "the person was asked and chose to leave them here, so do not save them to a "
            "file and do not copy them anywhere else."
            if leave_unsaved
            else "The checkout is not archived; its unsaved work is captured instead."
        )
        count = "two" if leave_unsaved else "three"
        steps.append(
            "\n".join(
                [
                    f"{step('record the repository state.')} Run these {count} commands, in order:",
                    *git_commands,
                    "NEVER RUN GIT COMMIT, GIT PUSH, GIT STASH, OR ANY OTHER COMMAND THAT "
                    "CHANGES THIS REPOSITORY'S HISTORY, INDEX, WORKING TREE, OR REMOTE. "
                    f"{disposition} Leave the tree exactly as you found it.",
                ]
            )
        )

    steps.append(
        "\n".join(
            [
                f"{step('build the archive.')} The shell starts in /, and the archive must be "
                "written flat into the outputs directory. Run this shell block "
                "exactly as written:",
                "  set -e",
                "  cd /",
                f"  mkdir -p {home_dir}/work {CHECKPOINT_SCRATCH_DIR}",
                f"  rm -f {_BUILT_MARKER_PATH}",
                f"  if [ -f {CHECKPOINT_BUNDLE_MOUNT_PATH} ]; then cp "
                f"{CHECKPOINT_BUNDLE_MOUNT_PATH} {home_dir}/inherited-handoff.tar.gz; fi",
                tar_command + f" && touch {_BUILT_MARKER_PATH}",
                "Do not omit oversized task files: if the archive exceeds the cap, the "
                "next step reports that the files could not be carried.",
            ]
        )
    )

    complete_conditions = [f"[ -f {_BUILT_MARKER_PATH} ]"]
    if repo_mount_path is not None and not leave_unsaved:
        complete_conditions.append(f"[ -f {shlex.quote(home_dir)}/{REPO_STATE_DIR}/complete ]")
    show: list[str] = [
        f"{step('check the size, then show the result.')} Run:",
        f"  if ! ( {' && '.join(complete_conditions)} ); then rm -f {archive_path}; "
        f"echo {HANDOFF_INCOMPLETE_MARKER}; else "
        f'size=$(stat -c %s {archive_path}); if [ "$size" -gt {max_bundle_bytes} ]; '
        f'then rm -f {archive_path}; echo "{HANDOFF_TOO_LARGE_MARKER} $size"; '
        f"else ls -l {archive_path}; fi; fi",
    ]
    if repo_mount_path is not None:
        show.append(f"  git -C {repo_mount_path} rev-parse HEAD")
    show.append(
        f"The archive cannot be carried above {max_bundle_mib} MiB, so that command deletes it "
        f"and prints {HANDOFF_TOO_LARGE_MARKER} instead. That is a complete answer: do not "
        "retry, shrink or rebuild it. A failed capture prints HANDOFF_INCOMPLETE; "
        "include that line in your reply even if other commands succeeded."
    )
    steps.append("\n".join(show))

    steps.append(
        f"{step('reply')} with the output of the commands above. A short note alongside it is "
        "fine; daimon reads the output, not the prose."
    )
    return "\n\n".join(sections + steps)


def checkpoint_head_lines(reply: str) -> tuple[str | None, str | None]:
    """The first and last commit hashes echoed by a checkpoint reply.

    The prompt asks for ``rev-parse HEAD`` before and after the archive step;
    two different hashes mean the session committed despite the prohibition,
    which the caller records rather than prevents. Returns ``(None, None)``
    when no hash was echoed and ``(hash, None)`` when only one was, so
    "unknown" is never mistaken for "unchanged".
    """

    hashes = _SHA1_LINE.findall(reply)
    if not hashes:
        return (None, None)
    if len(hashes) == 1:
        return (hashes[0], None)
    return (hashes[0], hashes[-1])


def checkpoint_too_large_bytes(reply: str) -> int | None:
    """The size the checkpoint turn reported when it rejected its own archive.

    The prompt's last step deletes an over-cap archive and prints
    ``HANDOFF_TOO_LARGE <bytes>``; matching a whole line means a reply that
    merely echoes the command it was given cannot be read as the outcome.
    Returns None when no such line was printed.
    """

    match = _TOO_LARGE_LINE.search(reply)
    return None if match is None else int(match.group(1))

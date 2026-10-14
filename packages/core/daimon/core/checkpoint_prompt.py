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
- The loose non-hidden files directly under ``/`` travel too: ``/`` is where
  the shell starts, and no environment setting moves it, so a file written
  "in your working directory" lands there (observed live). Only files, never
  ``/``'s directories, which are the system itself.
- ``/tmp/work`` is an archived root too, minus its own dot entries. It is
  not where daimon would choose to put working files, but it is where an
  agent asked to "create notes.md" reached for first (observed live), and a
  root nothing writes to costs nothing.
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
#: archive is over the cap even after the partial rebuild. Such an archive
#: never reaches the old session's outputs.
HANDOFF_TOO_LARGE_MARKER = "HANDOFF_TOO_LARGE"

#: Printed once per file the partial rebuild left out, as ``<marker> /<path>``
#: on a line of its own, so the successor is told exactly what to regenerate.
HANDOFF_LEFT_OUT_MARKER = "HANDOFF_LEFT_OUT"

#: Printed with a count when more files were left out than are listed one by one.
HANDOFF_LEFT_OUT_MORE_MARKER = "HANDOFF_LEFT_OUT_MORE"

# A long list costs reply tokens on a billed turn and the successor's prompt;
# the largest files come first, and they are the ones worth naming.
_LEFT_OUT_LISTED = 50

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

# Scratch files, outside every archived root (``/tmp`` itself is not archived,
# only ``/tmp/work``): the archive while tar writes it, the members with sizes,
# and the files the partial rebuild drops. The archive is copied into the
# outputs directory only when final, so that mount only ever sees the final
# name written once, never a rename. All share the bundle prefix, so one glob
# removes them.
_SCRATCH_ARCHIVE_DIR = "/tmp"
_MEMBER_SIZES_PATH = "/tmp/daimon-handoff-sizes"
_LEFT_OUT_LIST_PATH = "/tmp/daimon-handoff-left-out.txt"

#: The third archived root, after ``$HOME`` and the outputs directory. Only this
#: subdirectory of ``/tmp``: the base image ships ~60 MB of non-hidden browser
#: and compile-cache trees directly under ``/tmp`` (observed on staging), which
#: pushed an otherwise empty archive over the 20 MiB cap.
CHECKPOINT_SCRATCH_DIR = "/tmp/work"

_SHA1_LINE = re.compile(r"^\s*([0-9a-f]{40})\s*$", re.MULTILINE)

_TOO_LARGE_LINE = re.compile(rf"^\s*{HANDOFF_TOO_LARGE_MARKER}\s+(\d+)\s*$", re.MULTILINE)

_LEFT_OUT_LINE = re.compile(rf"^\s*{HANDOFF_LEFT_OUT_MARKER} (/.*?)\s*$", re.MULTILINE)

_LEFT_OUT_MORE_LINE = re.compile(rf"^\s*{HANDOFF_LEFT_OUT_MORE_MARKER}\s+(\d+)\s*$", re.MULTILINE)


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
    default (and ``"copy"``) captures them: the patch, the untracked list, and
    the checkout itself all travel in the archive. ``"leave"`` is the person
    saying those changes stay where they are, so the prompt neither captures
    them nor packs the checkout — the successor clones the repository fresh,
    and the copy that promises "the uncommitted changes stay in the old
    checkout" stays true.
    """

    archive_path = f"{CHECKPOINT_OUTPUTS_DIR}/{handoff_filename(transfer_id)}"
    max_bundle_bytes = max_bundle_mib * 1024 * 1024
    leave_unsaved = repo_mount_path is not None and unsaved_work == "leave"
    home_root = _relative_to_root(home_dir)
    outputs_root = _relative_to_root(CHECKPOINT_OUTPUTS_DIR)
    scratch_root = _relative_to_root(CHECKPOINT_SCRATCH_DIR)
    roots = [home_root, outputs_root, scratch_root]
    if repo_mount_path is not None and not leave_unsaved:
        roots.append(_relative_to_root(repo_mount_path))
    roots_argument = " ".join(roots)

    # One find decides what travels, for tar and for the size count alike, so the
    # drop list is exactly what was archived. A pruned path is never listed; tar
    # gets the rest by name and does not recurse. The first two prunes keep only
    # the loose non-hidden files of `.`, where the shell starts; patterns are
    # anchored at a root, and `*` matches `/`.
    prune_lines = [
        [
            "-path './*' ! -type f",
            "-path './.*'",
            f"-type f -size +{max_bundle_mib}M",
        ],
        # Every dot entry directly under $HOME: .ssh, and the ~100 MB of toolchain
        # caches the base image ships (.bun/.cargo/.rustup/.gradle/.npm/.local/
        # .config), while a dotfile deeper inside the task's own work travels. The
        # scratch directory's dot entries are the sandbox's sockets and locks. Then
        # the bundle being written, and any bundle a previous transfer left.
        [
            f"-path '{home_root}/.*'",
            f"-path '{scratch_root}/.*'",
            f"-path '{outputs_root}/{HANDOFF_FILENAME_PREFIX}*'",
        ],
        [f"-path '{_relative_to_root(path)}'" for path in CHECKPOINT_EXCLUDED_PATHS],
        [f"-path '*/{glob}'" for glob in CHECKPOINT_EXCLUDED_GLOBS] + ["-path '*.env'"],
    ]
    first, *rest = (" -o ".join(line) for line in prune_lines)
    members_function = "\n".join(
        [f"  members() {{ find . {roots_argument} -mindepth 1 \\( {first} \\"]
        + [f"    -o {line} \\" for line in rest]
        + ['    \\) -prune -o "$@"; }']
    )
    scratch_path = f"{_SCRATCH_ARCHIVE_DIR}/{handoff_filename(transfer_id)}"
    # The left-out list goes to tar as anchored literal names, one per line.
    pack_function = (
        f"  pack() {{ members -printf '%p\\0' | tar czf {scratch_path} --no-recursion "
        f"--anchored --no-wildcards --exclude-from={_LEFT_OUT_LIST_PATH} --null -T -; }}"
    )
    # Over the cap, the archive is rebuilt once without its largest members
    # until the rest, counted uncompressed with 1 KiB each for the tar header
    # and padding, fits 5% under the cap; the margin covers directory headers
    # and gzip's worst case, so small working files always travel. The size
    # check after the rebuild still guards the cap.
    # Sizes are counted per inode (`w`), since tar stores a hard-linked file's
    # bytes once however many of its names are archived, and an inode's names
    # (`n`) are dropped together or it would travel under the one kept. A name
    # tar cannot be told keeps its inode (`k`): a newline would split it, and
    # trailing whitespace is trimmed.
    budget = max_bundle_bytes - max_bundle_bytes // 20
    listed = _LEFT_OUT_LISTED
    size_check = [
        f'  size=$(stat -c %s {scratch_path}); if [ "$size" -gt {max_bundle_bytes} ]; then',
        f"    members ! -type d -printf '%D:%i %s %p\\0' > {_MEMBER_SIZES_PATH}",
        "    declare -A w n k; total=0; "
        "while IFS= read -r -d '' e; do i=${e%% *}; e=${e#* }; "
        "p=${e#* }; w[$i]=$((${w[$i]-${e%% *}} + 1024)); n[$i]+=$p$'\\n'",
        f"      [[ $p == *$'\\n'* || $p == *[[:space:]] ]] && k[$i]=1; done < {_MEMBER_SIZES_PATH}",
        '    for i in "${!w[@]}"; do total=$((total + ${w[$i]})); echo "${w[$i]} $i"; '
        f"done > {_MEMBER_SIZES_PATH}",
        f'    while [ "$total" -gt {budget} ] && read -r z i; do [ -n "${{k[$i]-}}" ] || '
        '{ total=$((total - z)); printf %s "${n[$i]}"; }',
        f"    done < <(sort -rn {_MEMBER_SIZES_PATH}) > {_LEFT_OUT_LIST_PATH}",
        f"    sed 's|^\\./||; s|^|{HANDOFF_LEFT_OUT_MARKER} /|; {listed}q' {_LEFT_OUT_LIST_PATH}",
        f'    c=$(wc -l < {_LEFT_OUT_LIST_PATH}); if [ "$c" -gt {listed} ]; then '
        f'echo "{HANDOFF_LEFT_OUT_MORE_MARKER} $((c - {listed}))"; fi',
        f"    pack; size=$(stat -c %s {scratch_path})",
        "  fi",
        f'  if [ "$size" -gt {max_bundle_bytes} ]; then echo "{HANDOFF_TOO_LARGE_MARKER} $size"; '
        f"else cp {scratch_path} {archive_path} && ls -l {archive_path} "
        f"|| rm -f {archive_path}; fi",
        f"  rm -f {_SCRATCH_ARCHIVE_DIR}/{HANDOFF_FILENAME_PREFIX}*",
    ]

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
        f"{home_dir} and {CHECKPOINT_SCRATCH_DIR}, the files directly in / (where the shell "
        "starts), the outputs directory, and the working "
        "repository if one is mounted. Credential mounts, hidden directories and language "
        "toolchains and their caches stay behind. The commands below already exclude them; "
        "run them as written.",
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
            git_commands += [
                f"  git -C {repo_mount_path} diff HEAD > {home_dir}/uncommitted.patch",
                f"  git -C {repo_mount_path} ls-files --others --exclude-standard"
                f" > {home_dir}/untracked.txt",
            ]
        disposition = (
            "The uncommitted changes in this checkout are deliberately being left behind: "
            "the person was asked and chose to leave them here, so do not save them to a "
            "file and do not copy them anywhere else."
            if leave_unsaved
            else "The uncommitted work is captured as a patch on purpose."
        )
        count = "two" if leave_unsaved else "four"
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

    archive_commands = [
        f"{step('build the archive and check its size.')} Run these commands in one shell, in "
        "order, exactly as written:",
        "  cd /",
        f"  mkdir -p {home_dir}/work {CHECKPOINT_SCRATCH_DIR}",
        f"  : > {_LEFT_OUT_LIST_PATH}",
        members_function,
        pack_function,
        "  pack",
        *size_check,
    ]
    if repo_mount_path is not None:
        archive_commands.append(f"  git -C {repo_mount_path} rev-parse HEAD")
    archive_commands.append(
        "members lists what travels. The archive cannot "
        f"be carried above {max_bundle_mib} MiB: then the commands rebuild it once without the "
        f"largest files, printing {HANDOFF_LEFT_OUT_MARKER} for each, and if it is still too "
        f"large delete it and print {HANDOFF_TOO_LARGE_MARKER}. That is a complete answer: do "
        "not edit the commands, and do not retry, shrink or rebuild the archive yourself."
    )
    steps.append("\n".join(archive_commands))

    steps.append(
        f"{step('reply')} with the output of the commands above, every "
        f"{HANDOFF_LEFT_OUT_MARKER} line as printed. A short note alongside it is fine; daimon "
        "reads the output, not the prose."
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


def checkpoint_left_out(reply: str) -> tuple[tuple[str, ...], int]:
    """The files the partial rebuild left out: absolute paths, then how many more.

    Whole lines only, as for :func:`checkpoint_too_large_bytes`: the command
    that prints the marker never starts a line with it, so an echo of the
    prompt reads as nothing left out.
    """

    paths = tuple(dict.fromkeys(_LEFT_OUT_LINE.findall(reply)))
    more = _LEFT_OUT_MORE_LINE.search(reply)
    return paths, 0 if more is None else int(more.group(1))

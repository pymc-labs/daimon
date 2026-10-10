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

import json
import re
import shlex
import uuid
from textwrap import indent
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
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    "dist",
    "build",
    ".next",
    ".tox",
    "coverage",
)

_BUILT_MARKER_PATH = "/tmp/daimon-handoff-built"

#: Where the git step saves what a fresh mount of the repository cannot give
#: back: its remote, HEAD and branch, the commits on no remote
#: (`local-commits.bundle`) and its untracked and ignored files
#: (`files.tar`). The binary patch stays at `$HOME/uncommitted.patch`.
REPO_STATE_DIR = "repo-state"

# These scripts are sent as readable heredocs, with no nested shell quoting.
# The inventory excludes reproducible caches and records every size omission.
_REPO_FILE_LIST_SCRIPT = r"""import json, os, subprocess, sys
repo, cap, inventory, omitted = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
caches = set(sys.argv[5:]) | {'.git'}
paths = subprocess.check_output(['git', '-C', repo, 'ls-files', '-z', '--others']).split(b'\0')
total = 0
with open(inventory, 'wb') as listing, open(omitted, 'a') as skipped:
    for path in sorted(p for p in paths if p):
        name = os.fsdecode(path)
        if name.endswith('.env') or caches.intersection(name.split('/')):
            continue
        size = os.lstat(os.path.join(repo, name)).st_size
        if total + size > cap:
            skipped.write(json.dumps(os.path.join(repo, name)) + '\n')
            continue
        listing.write(path + b'\0')
        total += size
"""

# Merge the inherited archive and the current files by member path. Current
# files win; old repository restoration artifacts get one stable, content-
# addressed directory only when a newer capture differs. Never add the mounted
# archive itself. Nothing is extracted into (or changes) the working checkout.
_ARCHIVE_SCRIPT = r"""import copy, hashlib, io, json, os, pathlib, sys, tarfile
home, outputs, scratch, inherited, archive, marker, omitted, repo, cap = sys.argv[1:10]
cap = int(cap)
roots = [pathlib.Path(p) for p in (home, outputs, scratch)]
caches = set(sys.argv[10:])
excluded = EXCLUDED_PATHS
def allowed(name):
    path = pathlib.PurePosixPath('/' + name)
    if '..' in path.parts or path.name.endswith('.env') or path.name == 'inherited-handoff.tar.gz':
        return False
    if any(str(path) == p or str(path).startswith(p + '/') for p in excluded):
        return False
    if repo and (str(path) == repo or str(path).startswith(repo + '/')):
        return False
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if relative.parts and relative.parts[0].startswith('.') and root != roots[1]:
            return False
        if caches.intersection(relative.parts):
            return False
        if '.git' in relative.parts and 'objects' in relative.parts:
            return False
        return not (root == roots[1] and path.name.startswith('daimon-handoff-'))
    return False
current = {}
for root in roots:
    root.mkdir(parents=True, exist_ok=True)
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if allowed(str(pathlib.Path(base, d)).lstrip('/')))
        paths = [pathlib.Path(base)] + [pathlib.Path(base, n) for n in sorted(files)]
        paths += [pathlib.Path(base, d) for d in dirs if pathlib.Path(base, d).is_symlink()]
        for path in paths:
            name = str(path).lstrip('/')
            if allowed(name):
                current[name] = path
previous = tarfile.open(inherited) if os.path.isfile(inherited) else None
old = {m.name: m for m in previous.getmembers() if allowed(m.name)} if previous else {}
prefix = home.lstrip('/') + '/'
def state_content(name, data):
    if name.endswith('/files.tar'):
        with tarfile.open(fileobj=io.BytesIO(data)) as files:
            digest = hashlib.sha256()
            for member in sorted(files.getmembers(), key=lambda m: m.name):
                digest.update((member.name + member.linkname).encode() + str(member.mode).encode())
                if member.isfile():
                    digest.update(files.extractfile(member).read())
            return digest.digest()
    return data
state_names = {
    n for n, m in old.items() if m.isfile() and (n.startswith(prefix + 'repo-state/')
    or n in (prefix + 'uncommitted.patch', prefix + 'untracked.txt'))
}
if state_names and any(n in current for n in state_names):
    digest = hashlib.sha256()
    same = True
    for name in sorted(state_names):
        data = state_content(name, previous.extractfile(old[name]).read())
        digest.update(name[len(prefix):].encode() + b'\0' + data)
        present = name in current and current[name].is_file()
        same = same and present and state_content(name, current[name].read_bytes()) == data
    if not same:
        saved = prefix + 'prior-repo-state/' + digest.hexdigest()[:16] + '/'
        for name in state_names:
            member = old.pop(name)
            renamed = saved + name[len(prefix):]
            old[renamed] = copy.copy(member)
            old[renamed].name = renamed
            old[renamed].pax_headers = member.pax_headers.copy()
            old[renamed].pax_headers.pop("path", None)
skipped = []
if os.path.exists(omitted):
    skipped = [json.loads(line) for line in pathlib.Path(omitted).read_text().splitlines()]
members = {}
for name in sorted(current.keys() | old.keys()):
    path = current.get(name)
    if path:
        with tarfile.open(os.devnull, 'w') as info:
            member = info.gettarinfo(str(path), arcname=name)
    else:
        member = old[name]
    if member.isfile() and member.size > cap:
        skipped.append('/' + name)
    else:
        members[name] = member
if skipped:
    with open(home + '/HANDOFF.md', 'a') as note:
        note.write('\nNot carried (size limit):\n' + '\n'.join(skipped) + '\n')
    for name in skipped:
        print('HANDOFF_OMITTED ' + json.dumps(name))
with tarfile.open(archive, 'w:gz', dereference=False) as bundle:
    for name, member in members.items():
        path = current.get(name)
        if path:
            bundle.add(str(path), arcname=name, recursive=False)
        elif member.isfile():
            original = next(m for m in previous.getmembers() if m.offset_data == member.offset_data)
            bundle.addfile(member, previous.extractfile(original))
        else:
            bundle.addfile(member)
if previous:
    previous.close()
pathlib.Path(marker).touch()
"""

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
    omitted_path = _BUILT_MARKER_PATH + ".omitted"
    cache_arguments = " ".join(
        shlex.quote(p) for p in CHECKPOINT_EXCLUDED_GLOBS if p != ".git/objects"
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

    steps.append(
        "\n".join(
            [
                step("start the capture."),
                f"  rm -f {shlex.quote(_BUILT_MARKER_PATH)} {shlex.quote(omitted_path)}",
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
                f"python3 - {repo} {max_bundle_bytes} {state}/files.list "
                f"{shlex.quote(omitted_path)} "
                f"{cache_arguments} <<'DAIMON_FILES'",
                _REPO_FILE_LIST_SCRIPT.rstrip(),
                "DAIMON_FILES",
                f"tar -C {repo} --null -T {state}/files.list -cf {state}/files.tar",
                f"touch {state}/complete",
            ]
            git_commands.append(
                "  bash -e -o pipefail <<'DAIMON_REPO'\n"
                + indent("\n".join(capture), "  ")
                + "\n  DAIMON_REPO"
            )
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
                f"  checkpoint_failed() {{ rm -f {shlex.quote(archive_path)}; "
                f"echo {HANDOFF_INCOMPLETE_MARKER}; }}",
                "  trap checkpoint_failed ERR",
                "  cd /",
                f"  mkdir -p {home_dir}/work {CHECKPOINT_SCRATCH_DIR}",
                f"  rm -f {_BUILT_MARKER_PATH}",
                "  python3 - "
                + " ".join(
                    shlex.quote(p)
                    for p in (
                        home_dir,
                        CHECKPOINT_OUTPUTS_DIR,
                        CHECKPOINT_SCRATCH_DIR,
                        CHECKPOINT_BUNDLE_MOUNT_PATH,
                        archive_path,
                        _BUILT_MARKER_PATH,
                        omitted_path,
                        repo_mount_path or "",
                        str(max_bundle_bytes),
                    )
                )
                + f" {cache_arguments} <<'DAIMON_ARCHIVE'",
                indent(
                    _ARCHIVE_SCRIPT.replace(
                        "EXCLUDED_PATHS", repr(CHECKPOINT_EXCLUDED_PATHS)
                    ).rstrip(),
                    "  ",
                ),
                "  DAIMON_ARCHIVE",
                "Inherited files are merged at their original paths; current files win. "
                "No inherited archive is embedded. Oversized files are skipped and named "
                "with HANDOFF_OMITTED; all other files still travel when the bundle fits.",
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


def checkpoint_omitted_files(reply: str) -> tuple[str, ...]:
    """Size omissions explicitly emitted by the checkpoint shell, never command echoes."""
    files: list[str] = []
    for line in reply.splitlines():
        line = line.strip()
        if not line.startswith("HANDOFF_OMITTED "):
            continue
        try:
            path = json.loads(line.removeprefix("HANDOFF_OMITTED "))
        except json.JSONDecodeError:
            path = "files named in an unreadable checkpoint omission list"
        if not isinstance(path, str):
            path = "files named in an unreadable checkpoint omission list"
        files.append(path)
    return tuple(dict.fromkeys(files))

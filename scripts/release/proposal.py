"""Release proposal: what promoting a staging-deployed SHA would ship, and how risky it is.

`tier` is a pure function over a `Span`; everything else gathers facts with `git` and
`gh` and renders markdown. Prints nothing when the CI run's post-deploy smoke step did
not succeed (a superseded or failed deploy proposes nothing). Read-only: it never
dispatches or edits anything. Usage: proposal.py --repo OWNER/NAME --run CI_RUN_ID
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HIGH, LOW = "high", "low"
MAX_PRS = 20
MAX_LISTED = 40
HIGH_PREFIXES = (".github/workflows/", "deploy/")
CORE_PREFIX = "packages/core/"
CORE_PATTERNS = (
    "*billing*", "*pricing*", "usage_*", "*auth*", "*access*", "*identity*",
    "*vault*", "*purge*", "*erasure*", "privacy*", "tenant_*",
)  # fmt: skip
SMOKE_JOB, SMOKE_STEP = "Deploy to GCP (staging)", "Post-deploy smoke turn"
VERSIONS = "packages/core/alembic/versions/"
_PR_MERGE = re.compile(r"^Merge pull request #(\d+)")
_PR_SQUASH = re.compile(r"\(#(\d+)\)$")
_PROMOTE = re.compile(r"^Promote ([0-9a-f]{40})$")


@dataclass(frozen=True)
class Migration:
    file: str
    schema: str  # expand | contract
    downgrade: str  # safe | destructive | unsupported | undeclared


@dataclass(frozen=True)
class Span:
    prs: int
    migrations: tuple[Migration, ...]
    paths: tuple[str, ...]
    bad_signals: tuple[str, ...]


def sensitive_path(path: str) -> bool:
    if path.startswith(HIGH_PREFIXES):
        return True
    parts = path.split("/")
    if not path.startswith(CORE_PREFIX) or "tests" in parts:
        return False
    return any(fnmatch.fnmatchcase(p.lower(), pat) for p in parts[2:] for pat in CORE_PATTERNS)


def tier(span: Span) -> tuple[str, list[str]]:
    """`(high|low, reasons)`; low has no reasons."""
    reasons = [f"contract migration `{m.file}`" for m in span.migrations if m.schema == "contract"]
    reasons += [
        f"`downgrade: unsupported` in `{m.file}`"
        for m in span.migrations
        if m.downgrade == "unsupported"
    ]
    if span.prs > MAX_PRS:
        reasons.append(f"{span.prs} PRs (more than {MAX_PRS})")
    reasons += [f"signal not green: {s}" for s in span.bad_signals]
    reasons += [f"sensitive path `{p}`" for p in span.paths if sensitive_path(p)][:5]
    return (HIGH if reasons else LOW), reasons


def smoke_succeeded(jobs: list[dict]) -> bool:
    """True when the staging deploy's smoke step concluded `success` in this CI run."""
    return any(
        j["name"].startswith(SMOKE_JOB) and s["name"] == SMOKE_STEP and s["conclusion"] == "success"
        for j in jobs
        for s in j.get("steps", [])
    )


def production_sha(promote_runs: list[dict]) -> str | None:
    """The newest successful Promote run's title carries the SHA (newest first)."""
    for run in promote_runs:
        m = _PROMOTE.match(run["displayTitle"])
        if m:
            return m.group(1)
    return None


def _run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def _gh_json(*args: str) -> object:
    return json.loads(_run("gh", *args))


def _marker(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(\w+)", text, re.MULTILINE)
    return m.group(1) if m else None


def _migrations(prod: str, cand: str) -> tuple[Migration, ...]:
    added = _run(
        "git", "diff", "--diff-filter=A", "--name-only", prod, cand, "--", VERSIONS
    ).split()
    added = sorted(f for f in added if f.endswith(".py"))
    if not added:
        return ()
    with tempfile.TemporaryDirectory() as tmp:
        files, downgrades = [], {}
        for f in added:  # candidate content, classified by the checked-out linter
            text = _run("git", "show", f"{cand}:{f}")
            dest = Path(tmp) / Path(f).name
            dest.write_text(text, encoding="utf-8")
            files.append(str(dest))
            downgrades[str(dest)] = _marker(text, "downgrade") or "undeclared"
        out = _run(
            sys.executable,
            str(Path(__file__).parents[1] / "lint_migrations.py"),
            "--classify",
            *files,
        )
    classes = dict(line.rsplit(": ", 1) for line in out.splitlines())
    return tuple(Migration(Path(f).name, classes[f], downgrades[f]) for f in files)


def _prs(prod: str, cand: str) -> list[tuple[int, str]]:
    log = _run("git", "log", "--first-parent", "--format=%s%x1f%b%x1e", f"{prod}..{cand}")
    prs = []
    for rec in filter(None, (r.strip() for r in log.split("\x1e"))):
        subject, _, body = rec.partition("\x1f")
        if m := _PR_MERGE.match(subject):
            lines = [x for x in body.splitlines() if x.strip()]
            prs.append((int(m[1]), lines[0] if lines else subject))
        elif m := _PR_SQUASH.search(subject):
            prs.append((int(m[1]), subject[: m.start()].strip()))
    return prs


def _fragments(prod: str, cand: str) -> list[str]:
    names = _run(
        "git", "diff", "--diff-filter=A", "--name-only", prod, cand, "--", "changes/"
    ).split()
    out = []
    for n in sorted(names):
        if n.endswith(".md"):
            text = _run("git", "show", f"{cand}:{n}").strip()
            out.append(f"`{n}`: {text.splitlines()[0][:160] if text else ''}")
    return out


def _listed(items: list[str]) -> list[str]:
    more = len(items) - MAX_LISTED
    return [*items[:MAX_LISTED], f"- ...and {more} more"] if more > 0 else items


def build(repo: str, run_id: str) -> str:
    run = _gh_json("api", f"repos/{repo}/actions/runs/{run_id}")
    out = _run(
        "gh", "api", "--paginate", f"repos/{repo}/actions/runs/{run_id}/jobs", "--jq", ".jobs[]"
    )
    jobs = [json.loads(line) for line in out.splitlines()]
    if not smoke_succeeded(jobs):  # superseded, no smoke needed, or failed
        return ""
    cand = run["head_sha"]
    promotes = _gh_json(
        *("run", "list", "-R", repo, "--workflow", "promote.yml", "--status", "success"),
        *("--limit", "30", "--json", "displayTitle"),
    )
    prod = production_sha(promotes)
    head = f"## Release proposal for `{cand}`\n\n"
    if prod is None:
        return (
            head
            + "Production SHA unknown: no successful Promote run is titled `Promote <sha>`.\n"
            + "Tier: **high**.\n"
        )
    ahead = int(_run("git", "rev-list", "--count", f"{prod}..{cand}"))
    if ahead == 0:
        return head + f"Nothing to promote: production (`{prod}`) already contains this SHA.\n"

    canaries = _gh_json("run", "list", "-R", repo, "--workflow", "canary.yml", "--limit", "30",
                        "--json", "conclusion,status,createdAt,url")  # fmt: skip
    since = [c for c in canaries if c["createdAt"] >= run["updated_at"]]
    bad = []
    if run["conclusion"] != "success":
        bad.append(f"main CI run concluded `{run['conclusion']}`")
    bad += [
        f"canary run {c['url']} `{c['conclusion']}`"
        for c in since
        if c["conclusion"] in ("failure", "timed_out")
    ]
    prs = _prs(prod, cand)
    reverts = _run(
        "git", "log", "--format=%h %s", "--grep=^Revert ", f"{prod}..{cand}"
    ).splitlines()
    migrations = _migrations(prod, cand)
    paths = tuple(_run("git", "diff", "--name-only", prod, cand).split())
    level, reasons = tier(Span(len(prs), migrations, paths, tuple(bad)))

    md = [head, f"Tier: **{level}**", *(f"- {r}" for r in reasons), ""]
    md.append(
        f"Production: `{prod}`. Span: {ahead} commits, {len(prs)} PRs, {len(reverts)} reverts.\n"
    )
    if reverts:
        md += ["Reverts:", *_listed([f"- {r}" for r in reverts]), ""]
    if prs:
        md += ["PRs:", *_listed([f"- #{n} {t}" for n, t in sorted(prs)]), ""]
    if migrations:
        md += ["| Migration | schema | downgrade |", "|---|---|---|"]
        md += [f"| `{m.file}` | {m.schema} | {m.downgrade} |" for m in migrations] + [""]
    frags = _fragments(prod, cand)
    if frags:
        md += ["Changelog fragments:", *_listed([f"- {f}" for f in frags]), ""]
    smoke = f"`{SMOKE_STEP}` succeeded"
    canary = (
        ", ".join(f"[{c['conclusion'] or c['status']}]({c['url']})" for c in since[:5])
        or "none yet"
    )
    md += ["Signals:", f"- main CI run: {run['html_url']} (`{run['conclusion']}`)", f"- {smoke}",
           f"- canary runs since: {canary}"]  # fmt: skip
    return "\n".join(md) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True)
    p.add_argument("--run", required=True, help="CI run id of the staging deploy")
    args = p.parse_args(argv)
    sys.stdout.write(build(args.repo, args.run))
    return 0


if __name__ == "__main__":
    sys.exit(main())

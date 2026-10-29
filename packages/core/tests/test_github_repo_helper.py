"""The mounted GitHub helper selects only a granted repository's placeholder."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from daimon.core.credential_env import github_repo_script_bytes, render_github_repo_map


def _run(
    script: Path, env: dict[str, str], *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        cwd=script.parent,
        check=False,
    )


def test_per_repo_helper_selects_exact_placeholder_and_configures_clone(tmp_path: Path) -> None:
    script = tmp_path / "github-repo.sh"
    script.write_bytes(github_repo_script_bytes())
    (tmp_path / "github-repos.tsv").write_bytes(
        render_github_repo_map(
            (("OwnerA/RepoA", "GH_TOKEN_I101_0"), ("OwnerB/RepoB", "GH_TOKEN_I202_0"))
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' "$GH_TOKEN" > "$CAPTURE_DIR/token"\n'
        'printf \'%s\\n\' "$@" > "$CAPTURE_DIR/args"\n'
        'if [ "$1" = repo ] && [ "$2" = clone ]; then\n'
        "  dir=${4:-${3##*/}}\n"
        '  git init -q "$dir"\n'
        '  git -C "$dir" remote add origin "https://github.com/$3.git"\n'
        "fi\n"
    )
    fake_gh.chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CAPTURE_DIR": str(capture),
            "GH_TOKEN_I101_0": "opaque-placeholder-A",
            "GH_TOKEN_I202_0": "opaque-placeholder-B",
            "GH_TOKEN": "wrong-alias",
        }
    )
    global_config = tmp_path / "global-gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(global_config),
            "credential.https://github.com.helper",
            "!f() { echo username=wrong; echo password=wrong-inherited-token; }; f",
        ],
        check=True,
    )
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )

    selected = _run(script, env, "ownera/REPOA.git", "api", "repos/OwnerA/RepoA")
    assert selected.returncode == 0
    assert (capture / "token").read_text() == "opaque-placeholder-A"
    assert (capture / "args").read_text().splitlines() == ["api", "repos/OwnerA/RepoA"]
    assert "opaque-placeholder" not in selected.stdout + selected.stderr

    selected_b = _run(script, env, "OWNERB/repob", "repo", "view", "OwnerB/RepoB")
    assert selected_b.returncode == 0
    assert (capture / "token").read_text() == "opaque-placeholder-B"
    assert "opaque-placeholder" not in selected_b.stdout + selected_b.stderr

    (capture / "token").unlink()
    denied = _run(script, env, "other/hidden", "api", "repos/other/hidden")
    assert denied.returncode == 2
    assert "other/hidden is not granted to this agent" in denied.stderr
    assert not (capture / "token").exists(), "gh must not run for an uncovered repo"
    assert "opaque-placeholder" not in denied.stdout + denied.stderr

    missing_variable = _run(
        script,
        {key: value for key, value in env.items() if key != "GH_TOKEN_I202_0"},
        "OwnerB/RepoB",
        "api",
        "repos/OwnerB/RepoB",
    )
    assert missing_variable.returncode == 2
    assert "OwnerB/RepoB is not granted to this agent" in missing_variable.stderr
    assert not (capture / "token").exists()

    for command in ("auth", "config", "alias", "extension", "secret", "variable"):
        blocked = _run(script, env, "OwnerB/RepoB", command, "token")
        assert blocked.returncode == 2
        assert f"gh {command} is unavailable" in blocked.stderr
        assert not (capture / "token").exists()
    for args in (
        ("--help=false", "auth", "token"),
        ("-R", "OwnerB/RepoB", "auth", "token"),
        ("--repo=OwnerB/RepoB", "api", "user"),
        ("--help",),
        ("unknown",),
    ):
        blocked = _run(script, env, "OwnerB/RepoB", *args)
        assert blocked.returncode == 2
        assert "usage: sh github-repo.sh" in blocked.stderr
        assert not (capture / "token").exists()

    clone_dir = tmp_path / "clone-b"
    clone = _run(script, env, "OwnerB/RepoB", "clone", str(clone_dir))
    assert clone.returncode == 0, clone.stderr
    assert (capture / "token").read_text() == "opaque-placeholder-B"
    helpers = subprocess.check_output(
        [
            "git",
            "-C",
            str(clone_dir),
            "config",
            "--local",
            "--get-all",
            "credential.https://github.com.helper",
        ],
        text=True,
    ).splitlines()
    assert helpers == ["", "!sh /mnt/session/uploads/github-repo.sh --git-credential"]
    assert (
        subprocess.check_output(
            ["git", "-C", str(clone_dir), "config", "--get", "credential.useHttpPath"],
            text=True,
        ).strip()
        == "true"
    )
    # Substitute the local test asset path for the sandbox mount path, keeping
    # the empty local entry that resets the inherited global helper.
    subprocess.run(
        [
            "git",
            "-C",
            str(clone_dir),
            "config",
            "--unset-all",
            "credential.https://github.com.helper",
        ],
        check=True,
    )
    for value in ("", f"!sh {script} --git-credential"):
        subprocess.run(
            [
                "git",
                "-C",
                str(clone_dir),
                "config",
                "--add",
                "credential.https://github.com.helper",
                value,
            ],
            check=True,
        )
    credential = subprocess.run(
        ["git", "-C", str(clone_dir), "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=OwnerB/RepoB.git\n\n",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert credential.returncode == 0
    assert "username=x-access-token\n" in credential.stdout
    assert "password=opaque-placeholder-B\n" in credential.stdout
    assert "wrong-inherited-token" not in credential.stdout
    for path in ("OwnerA/RepoA", "not/granted"):
        denied_git = subprocess.run(
            ["git", "-C", str(clone_dir), "credential", "fill"],
            input=f"protocol=https\nhost=github.com\npath={path}\n\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert denied_git.returncode != 0
        assert "password=" not in denied_git.stdout
        assert "wrong-inherited-token" not in denied_git.stdout
    assert "opaque-placeholder" not in clone.stdout + clone.stderr


def test_git_credential_mode_only_answers_exact_granted_https_path(tmp_path: Path) -> None:
    script = tmp_path / "github-repo.sh"
    script.write_bytes(github_repo_script_bytes())
    (tmp_path / "github-repos.tsv").write_bytes(
        render_github_repo_map((("OwnerB/RepoB", "GH_TOKEN_I202_0"),))
    )
    env = {**os.environ, "GH_TOKEN_I202_0": "opaque-placeholder-B"}
    valid = _run(
        script,
        env,
        "--git-credential",
        "get",
        stdin="protocol=https\nhost=github.com\npath=ownerb/REPOB.git\n\n",
    )
    assert valid.returncode == 0
    assert valid.stdout == "username=x-access-token\npassword=opaque-placeholder-B\n"
    for request in (
        "protocol=http\nhost=github.com\npath=OwnerB/RepoB\n\n",
        "protocol=https\nhost=evil.example\npath=OwnerB/RepoB\n\n",
        "protocol=https\nhost=github.com\npath=OwnerB/RepoB/extra\n\n",
        "protocol=https\nhost=github.com\npath=other/repo\n\n",
        "protocol=https\nhost=github.com\n\n",
    ):
        result = _run(script, env, "--git-credential", "get", stdin=request)
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""
    for method in ("store", "erase"):
        result = _run(script, env, "--git-credential", method, stdin="anything\n\n")
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""


def _push_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    """Script, env, capture dir and a clone of OwnerB/RepoB with a recording fake git."""
    script = tmp_path / "github-repo.sh"
    script.write_bytes(github_repo_script_bytes())
    (tmp_path / "github-repos.tsv").write_bytes(
        render_github_repo_map(
            (("OwnerA/RepoA", "GH_TOKEN_I101_0"), ("OwnerB/RepoB", "GH_TOKEN_I202_0"))
        )
    )
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = push ]; then\n'
        '    printf \'%s\\n\' "$@" > "$CAPTURE_DIR/push-args"\n'
        '    env > "$CAPTURE_DIR/push-env"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'exec {real_git} "$@"\n'
    )
    fake_git.chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    clone = tmp_path / "clone"
    subprocess.run([real_git, "init", "-q", "-b", "main", str(clone)], check=True)
    subprocess.run(
        [
            real_git,
            "-C",
            str(clone),
            "remote",
            "add",
            "origin",
            "https://github.com/ownerb/REPOB.git",
        ],
        check=True,
    )
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(
        [real_git, "-C", str(clone), *identity, "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
    )
    for branch in ("feature", "other/nested"):
        subprocess.run([real_git, "-C", str(clone), "branch", branch], check=True)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CAPTURE_DIR": str(capture),
            "GH_TOKEN_I101_0": "opaque-placeholder-A",
            "GH_TOKEN_I202_0": "opaque-placeholder-B",
            "GH_TOKEN": "wrong-alias",
            "GIT_CONFIG_GLOBAL": str(tmp_path / "global-gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return script, env, capture, clone


def _run_in(
    script: Path, env: dict[str, str], cwd: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), *args],
        text=True,
        capture_output=True,
        env=env,
        cwd=cwd,
        check=False,
    )


_PUSH_PREFIX = [
    "-c",
    "credential.https://github.com.helper=",
    "-c",
    "credential.https://github.com.helper=!sh /mnt/session/uploads/github-repo.sh --git-credential",
    "-c",
    "credential.useHttpPath=true",
]


def test_push_goes_to_origin_through_credential_helper_without_exposing_placeholder(
    tmp_path: Path,
) -> None:
    script, env, capture, clone = _push_fixture(tmp_path)
    recorded = capture / "push-args"

    pushed = _run_in(script, env, clone, "OWNERB/repob.git", "push", "feature", "other/nested")
    assert pushed.returncode == 0, pushed.stderr
    assert recorded.read_text().splitlines() == [
        *_PUSH_PREFIX,
        "push",
        "origin",
        "refs/heads/feature:refs/heads/feature",
        "refs/heads/other/nested:refs/heads/other/nested",
    ]
    pushed_env = (capture / "push-env").read_text()
    assert "GIT_TERMINAL_PROMPT=0" in pushed_env.splitlines()
    # The credential helper reads the mapped variable from the inherited
    # environment, so it is the only place the value appears.
    assert [line for line in pushed_env.splitlines() if "opaque-placeholder-B" in line] == [
        "GH_TOKEN_I202_0=opaque-placeholder-B"
    ]
    assert "x-access-token" not in pushed_env
    for text in (recorded.read_text(), pushed.stdout, pushed.stderr):
        assert "opaque-placeholder" not in text
        assert "x-access-token" not in text
        assert "wrong-alias" not in text
    assert "github.com/OwnerB" not in recorded.read_text()

    for flag in ("-q", "--quiet"):
        recorded.unlink()
        quiet = _run_in(script, env, clone, "OwnerB/RepoB", "push", flag, "feature")
        assert quiet.returncode == 0, quiet.stderr
        assert recorded.read_text().splitlines() == [
            *_PUSH_PREFIX,
            "push",
            "-q",
            "origin",
            "refs/heads/feature:refs/heads/feature",
        ]
        assert "opaque-placeholder" not in quiet.stdout + quiet.stderr


def test_git_credential_get_still_answers_nothing_for_unmapped_path(tmp_path: Path) -> None:
    script, env, _, clone = _push_fixture(tmp_path)
    for path in ("other/hidden.git", "OwnerA/RepoA.git"):
        result = subprocess.run(
            ["sh", str(script), "--git-credential", "get"],
            input=f"protocol=https\nhost=github.com\npath={path}\n\n",
            text=True,
            capture_output=True,
            env=env,
            cwd=clone,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""


def test_push_refuses_origin_mismatch_unmapped_repo_and_every_other_flag(
    tmp_path: Path,
) -> None:
    script, env, capture, clone = _push_fixture(tmp_path)
    pushed = capture / "push-args"

    mismatch = _run_in(script, env, clone, "OwnerA/RepoA", "push", "feature")
    assert mismatch.returncode == 2
    assert "not a clone of OwnerA/RepoA" in mismatch.stderr

    unmapped = _run_in(script, env, clone, "other/hidden", "push", "feature")
    assert unmapped.returncode == 2
    assert "other/hidden is not granted to this agent" in unmapped.stderr

    not_a_clone = _run_in(script, env, tmp_path / "bin", "OwnerB/RepoB", "push", "feature")
    assert not_a_clone.returncode == 2

    for flags in (
        ("--receive-pack=evil", "feature"),
        ("--exec=evil", "feature"),
        ("--repo=https://evil.example/x.git", "feature"),
        ("-o", "ci.skip", "feature"),
        ("--force", "feature"),
        ("-f", "feature"),
        ("--mirror",),
        ("-q", "-f", "feature"),
        ("-q", "--force", "feature"),
        ("feature", "--force"),
        ("feature", "-o", "x"),
        ("--", "feature"),
        ("-qf", "feature"),
        ("-u", "feature"),
        ("--quiet", "--quiet", "feature"),
        (),
        ("-q",),
        ("",),
        ("+feature",),
        ("+HEAD:refs/heads/main",),
        (":refs/heads/main",),
        ("+:",),
        (":",),
        ("refs/heads/*:refs/heads/*",),
        ("feature:main:extra",),
        ("-q", "+feature"),
        ("0000000000000000000000000000000000000000",),
        ("0000000000000000000000000000000000000000:refs/heads/main",),
        ("main:other",),
        ("HEAD",),
        ("@{-1}",),
        ("nonexistent",),
        ("feature", "nonexistent"),
        ("refs/heads/feature",),
        ("feature*",),
        ("feat?re",),
        ("feature^",),
        ("feature..other",),
    ):
        refused = _run_in(script, env, clone, "OwnerB/RepoB", "push", *flags)
        assert refused.returncode == 2, flags
        assert (
            "usage: sh github-repo.sh owner/repo push" in refused.stderr
            or "no local branch" in refused.stderr
        ), flags
        assert "opaque-placeholder" not in refused.stdout + refused.stderr
    assert not pushed.exists(), "git push must not run for any refused invocation"
    wrong_repo = _run_in(script, env, clone, "OwnerA/RepoA", "push", "-q", "feature")
    assert wrong_repo.returncode == 2
    assert not pushed.exists()
    assert "opaque-placeholder" not in mismatch.stdout + mismatch.stderr + unmapped.stderr


def test_push_names_the_missing_local_branch_and_accepts_only_branch_names(
    tmp_path: Path,
) -> None:
    script, env, capture, clone = _push_fixture(tmp_path)
    missing = _run_in(script, env, clone, "OwnerB/RepoB", "push", "nonexistent")
    assert missing.returncode == 2
    assert "no local branch nonexistent" in missing.stderr
    # A full ref name is not a branch name: refs/heads/refs/heads/feature does not exist.
    full_ref = _run_in(script, env, clone, "OwnerB/RepoB", "push", "refs/heads/feature")
    assert full_ref.returncode == 2
    assert "no local branch refs/heads/feature" in full_ref.stderr
    bad_format = _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature..other")
    assert bad_format.returncode == 2
    assert "usage: sh github-repo.sh owner/repo push" in bad_format.stderr
    assert not (capture / "push-args").exists()


def test_push_ignores_configured_push_refspec_and_uses_the_explicit_one(tmp_path: Path) -> None:
    script, env, capture, clone = _push_fixture(tmp_path)
    real_git = shutil.which("git")
    assert real_git is not None
    subprocess.run(
        [
            real_git,
            "-C",
            str(clone),
            "config",
            "remote.origin.push",
            "+refs/heads/feature:refs/heads/main",
        ],
        check=True,
    )
    pushed = _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature")
    assert pushed.returncode == 0, pushed.stderr
    assert (capture / "push-args").read_text().splitlines() == [
        *_PUSH_PREFIX,
        "push",
        "origin",
        "refs/heads/feature:refs/heads/feature",
    ]


def test_push_refuses_more_than_one_push_url(tmp_path: Path) -> None:
    script, env, capture, clone = _push_fixture(tmp_path)
    real_git = shutil.which("git")
    assert real_git is not None
    for url in ("https://github.com/ownerb/repob.git", "https://github.com/OwnerA/RepoA.git"):
        subprocess.run(
            [real_git, "-C", str(clone), "config", "--add", "remote.origin.pushurl", url],
            check=True,
        )
    # Two push URLs: the first matches the repo, the second does not.
    refused = _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature")
    assert refused.returncode == 2
    assert "more than one push URL" in refused.stderr
    assert not (capture / "push-args").exists()


def test_push_refuses_a_push_url_redirected_away_from_the_repo(tmp_path: Path) -> None:
    script, env, capture, clone = _push_fixture(tmp_path)
    real_git = shutil.which("git")
    assert real_git is not None
    redirects = (
        ("remote.origin.pushurl", "https://github.com/OwnerA/RepoA.git"),
        ("remote.origin.pushurl", "git@github.com:OwnerB/RepoB.git"),
        ("url.ssh://git@evil.example/.pushInsteadOf", "https://github.com/"),
        ("url.https://github.com/OwnerA/.insteadOf", "https://github.com/ownerb/"),
    )
    for key, value in redirects:
        subprocess.run([real_git, "-C", str(clone), "config", key, value], check=True)
        refused = _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature")
        assert refused.returncode == 2, (key, value)
        assert "opaque-placeholder" not in refused.stdout + refused.stderr
        subprocess.run([real_git, "-C", str(clone), "config", "--unset", key], check=True)
    assert not (capture / "push-args").exists()


_REAL_GIT = shutil.which("git") or ""
_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def _git(repo: Path, *args: str) -> str:
    """Run real git in `repo` and return stdout."""
    return subprocess.run(
        [_REAL_GIT, "-C", str(repo), *_IDENTITY, *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _remote_refs(bare: Path) -> dict[str, str]:
    """Every ref in the bare repo mapped to the object it points at."""
    listing = _git(bare, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ") for line in listing.splitlines())


def _real_push_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    """The push fixture, but `git push` really runs and lands in a local bare repo.

    The wrapper adds an `insteadOf` rewrite only to the push command, so the
    helper's own origin check still sees the GitHub URL while the transfer goes
    to the bare repo and every `remote.origin.*` / `push.*` setting still applies.
    The bare repo holds a `main` and a `feature` whose histories diverge from
    the clone's, plus a `stale` branch the clone does not have.
    """
    script, env, _, clone = _push_fixture(tmp_path)
    assert _REAL_GIT
    _git(clone, "checkout", "-q", "feature")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "local feature work")
    _git(clone, "checkout", "-q", "main")
    _git(clone, "branch", "-f", "other/nested", "feature")

    bare = tmp_path / "remote.git"
    subprocess.run([_REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run([_REAL_GIT, "init", "-q", "-b", "main", str(seed)], check=True)
    _git(seed, "commit", "-q", "--allow-empty", "-m", "remote main")
    _git(seed, "branch", "stale")
    _git(seed, "branch", "remote-feature")
    _git(seed, "checkout", "-q", "remote-feature")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "remote feature work")
    _git(
        seed,
        "push",
        "-q",
        str(bare),
        "main:refs/heads/main",
        "stale:refs/heads/stale",
        "remote-feature:refs/heads/remote-feature",
    )

    wrapper = tmp_path / "bin" / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = push ]; then\n'
        f"    exec {_REAL_GIT} -c url.{bare}.insteadOf=https://github.com/ownerb/REPOB.git "
        '"$@"\n'
        "  fi\n"
        "done\n"
        f'exec {_REAL_GIT} "$@"\n'
    )
    return script, env, clone, bare


def test_real_push_lands_the_named_branches_in_the_remote(tmp_path: Path) -> None:
    script, env, clone, bare = _real_push_fixture(tmp_path)
    before = _remote_refs(bare)

    pushed = _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature", "other/nested")
    assert pushed.returncode == 0, pushed.stderr
    feature = _git(clone, "rev-parse", "refs/heads/feature")
    assert _remote_refs(bare) == {
        **before,
        "refs/heads/feature": feature,
        "refs/heads/other/nested": feature,
    }


def test_real_push_ignores_a_configured_forcing_remapping_refspec(tmp_path: Path) -> None:
    script, env, clone, bare = _real_push_fixture(tmp_path)
    _git(clone, "config", "remote.origin.push", "+refs/heads/feature:refs/heads/main")
    before = _remote_refs(bare)

    pushed = _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature")
    assert pushed.returncode == 0, pushed.stderr
    after = _remote_refs(bare)
    assert after["refs/heads/main"] == before["refs/heads/main"]
    assert after == {**before, "refs/heads/feature": _git(clone, "rev-parse", "feature")}


def test_real_push_does_not_force_a_diverged_remote_branch(tmp_path: Path) -> None:
    script, env, clone, bare = _real_push_fixture(tmp_path)
    _git(clone, "config", "remote.origin.push", "+refs/heads/*:refs/heads/*")
    _git(clone, "config", "push.default", "matching")
    diverged = _git(bare, "rev-parse", "refs/heads/remote-feature")
    _git(bare, "update-ref", "refs/heads/feature", diverged)
    before = _remote_refs(bare)

    rejected = _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature")
    assert rejected.returncode != 0
    assert "[rejected]" in rejected.stderr
    assert _remote_refs(bare) == before


def test_real_push_with_mirror_config_deletes_and_overwrites_nothing(tmp_path: Path) -> None:
    script, env, clone, bare = _real_push_fixture(tmp_path)
    _git(clone, "config", "remote.origin.mirror", "true")
    before = _remote_refs(bare)
    assert {"refs/heads/main", "refs/heads/stale", "refs/heads/remote-feature"} <= set(before)

    _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature")
    after = _remote_refs(bare)
    assert set(before) <= set(after), "no remote ref may be deleted"
    assert {ref: oid for ref, oid in after.items() if ref in before} == before
    assert set(after) - set(before) <= {"refs/heads/feature"}


def test_real_push_follow_tags_adds_only_new_tags_for_the_pushed_commits(tmp_path: Path) -> None:
    script, env, clone, bare = _real_push_fixture(tmp_path)
    _git(clone, "config", "push.followTags", "true")
    _git(clone, "tag", "-a", "-m", "release", "v1", "feature")
    _git(clone, "checkout", "-q", "-b", "side", "main")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "unpushed side work")
    _git(clone, "tag", "-a", "-m", "elsewhere", "v0", "side")
    _git(clone, "checkout", "-q", "main")
    before = _remote_refs(bare)

    pushed = _run_in(script, env, clone, "OwnerB/RepoB", "push", "feature")
    assert pushed.returncode == 0, pushed.stderr
    after = _remote_refs(bare)
    assert {ref: oid for ref, oid in after.items() if ref in before} == before
    # Documented git behaviour: followTags adds annotated tags reachable from
    # the pushed branch. Tags are never moved or deleted, and `v0` (reachable
    # only from the unpushed `side` branch) stays local.
    assert set(after) - set(before) == {"refs/heads/feature", "refs/tags/v1"}

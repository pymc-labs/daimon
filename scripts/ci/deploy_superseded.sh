#!/usr/bin/env bash
# Whether a main deploy of <sha> is superseded: main has moved past it, so a newer
# CI run deploys a superset and this one must not roll staging back.
#
#   deploy_superseded.sh <sha> [remote] [branch]
#
# Prints `superseded=true|false`, `head=<sha>` and `reason=<why>` (GITHUB_OUTPUT
# lines). Superseded only when <sha> is NOT the branch head AND is provably its
# ancestor. Git answers first (a fetch, retried once, its errors logged); if git
# cannot, the GitHub API does (GH_TOKEN, GITHUB_REPOSITORY). Anything else deploys:
# the head itself, a commit not on the branch, or a history nothing can read.
set -u
sha="${1:?usage: deploy_superseded.sh <sha> [remote] [branch]}"
remote="${2:-origin}"
branch="${3:-main}"
ref="refs/remotes/$remote/$branch"

decide() { echo "superseded=$1"; echo "head=${2:-}"; echo "reason=$3"; exit 0; }

# The same answer from the API: main's head, then whether main is ahead of <sha>
# with nothing <sha> has that main lacks. Any failure deploys.
api_decide() {
  local repo="${GITHUB_REPOSITORY:-}" api_head cmp
  [ -n "$repo" ] && command -v gh >/dev/null || decide false "" fetch_failed
  api_head="$(gh api "repos/$repo/commits/$branch" --jq .sha)" || decide false "" fetch_failed
  [ "$sha" = "$api_head" ] && decide false "$api_head" head
  cmp="$(gh api "repos/$repo/compare/$sha...$api_head" --jq '[.status, .behind_by] | @tsv')" \
    || decide false "$api_head" fetch_failed
  [ "$cmp" = "$(printf 'ahead\t0')" ] && decide true "$api_head" api_ancestor
  decide false "$api_head" api_not_ancestor
}


fetch() { # fetch [extra git args]; the error text is kept in the job log
  git fetch --quiet --no-tags "$@" "$remote" "+refs/heads/$branch:$ref" || return 1
}
unshallow=""
[ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = true ] && unshallow=--unshallow

# Commits only (no trees or blobs) first: cheap on a shallow checkout. A second
# attempt drops the filter, which not every server or git accepts.
if ! fetch --filter=tree:0 $unshallow && ! { echo "deploy_superseded: retrying without --filter" >&2; fetch $unshallow; }; then
  echo "deploy_superseded: git fetch failed twice; asking the GitHub API" >&2
  api_decide
fi
head="$(git rev-parse --verify --quiet "$ref^{commit}")" || decide false "" no_head
[ "$sha" = "$head" ] && decide false "$head" head
if git merge-base --is-ancestor "$sha" "$head" 2>/dev/null; then
  decide true "$head" ancestor
fi
decide false "$head" not_ancestor

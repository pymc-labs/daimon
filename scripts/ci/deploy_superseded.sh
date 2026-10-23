#!/usr/bin/env bash
# Whether a main deploy of <sha> is superseded: main has moved past it, so a newer
# CI run deploys a superset and this one must not roll staging back.
#
#   deploy_superseded.sh <sha> [remote] [branch]
#
# Prints `superseded=true|false`, `head=<sha>` and `reason=<why>` (GITHUB_OUTPUT
# lines). Superseded only when <sha> is NOT the branch head AND is provably its
# ancestor. Anything else deploys: the head itself, a commit not on the branch,
# a failed fetch or a history that can't be read.
set -u
sha="${1:?usage: deploy_superseded.sh <sha> [remote] [branch]}"
remote="${2:-origin}"
branch="${3:-main}"
ref="refs/remotes/$remote/$branch"

decide() { echo "superseded=$1"; echo "head=${2:-}"; echo "reason=$3"; exit 0; }

# Commits only (no trees or blobs): enough to walk ancestry, cheap on a shallow checkout.
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = true ]; then
  git fetch --quiet --no-tags --filter=tree:0 --unshallow "$remote" "+refs/heads/$branch:$ref" \
    2>/dev/null || decide false "" fetch_failed
else
  git fetch --quiet --no-tags --filter=tree:0 "$remote" "+refs/heads/$branch:$ref" \
    2>/dev/null || decide false "" fetch_failed
fi
head="$(git rev-parse --verify --quiet "$ref^{commit}")" || decide false "" no_head
[ "$sha" = "$head" ] && decide false "$head" head
if git merge-base --is-ancestor "$sha" "$head" 2>/dev/null; then
  decide true "$head" ancestor
fi
decide false "$head" not_ancestor

#!/usr/bin/env bash
# Plan a selective staging deploy against the commit the last deploy recorded.
# Usage: deploy_plan.sh <workers-vm> <zone>
# A missing or unreadable record plans a full deploy, never a failure.
set -uo pipefail

base=$(gcloud compute instances describe "$1" --zone "$2" \
  --format='value(metadata.items.extract(deployed-sha).flatten())' 2>/dev/null) || base=""
if [[ ! "$base" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::notice::No deployed commit on record; deploying everything."
  base=""
elif ! git cat-file -e "$base^{commit}" 2>/dev/null \
  && ! git fetch --quiet --no-tags --depth=1 origin "$base"; then
  echo "::notice::Cannot fetch the deployed commit $base; deploying everything."
  base=""
fi
exec python3 scripts/ci/plan.py deploy --base "$base"

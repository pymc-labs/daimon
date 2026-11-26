- **Staging deploys wait for a quiet moment.** A merge to `main` deploys staging
  once its checks pass and at least `STAGING_QUIET_MINUTES` (repository
  variable, default 5, `0` for no wait) have passed, unless `main` has moved
  on by then, so a burst of merges deploys once, from the last commit. A deploy
  that cannot tell whether `main` has moved on now asks the GitHub API.

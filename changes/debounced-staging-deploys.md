- **Staging deploys wait for a quiet moment.** A burst of merges to `main` now
  deploys staging once, from the last commit, five minutes after the push
  (repository variable `STAGING_QUIET_MINUTES`, `0` for no wait). A deploy
  that cannot tell whether `main` has moved on now asks the GitHub API.

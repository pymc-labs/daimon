# GitHub grant lock order

`GrantLocks.tla` checks a panel batch and confirmation, two confirmations that
lock opposite agents, and request completion that sees a newly staged draft.
Grant writers take the transaction-scoped tenant lock before repository or
agent locks. TLC finds no deadlock across the fixed cases. The broken configs
omit that lock and reproduce a deadlock in each case.

## Not modelled

Confirmation racing background installation reconciliation uses installation
row before reconciliation row in both transactions. A concurrent database test
covers that ordering. Modelling this row-lock interleaving is a follow-up.

Run `TLA2TOOLS_JAR=/home/derick/scratch/daimon-os-devenv/tla2tools.jar FORMAL_CHECK_FILTER=github_grant_locks formal/check.sh` from the repository root. Verdicts and distinct-state counts are pinned in `formal/expected.tsv` with one TLC worker.

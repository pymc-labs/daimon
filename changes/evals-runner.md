- **Eval runs with spending caps.** Estimate a run, start it with a cap, or cancel it,
  from the web API, MCP tools or `daimon evals runs`. A cap past the operator's limits
  waits for the operator's approval. Items play the case's script on a simulated chat
  and stop at the cap, even mid-turn, and at their turn and time limits.
  `daimon evals worker` runs each item as a real turn of a copy of the arm's agent in its
  own sandbox, and the script presses its approve cards and receives its files.

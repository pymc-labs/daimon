# Conversational GitHub grant

Question: can the model cause a grant without a later yes by the same requester, before expiry, while grant permission still holds?

The model has two actors, three turns, a two-tick lifetime, and one proposal. `Propose` corresponds to `github_grant_proposals.propose`; `NextHuman` and `HumanYes` to `turn_origin.turn_origin` and `github_grant_proposals.resolve`; `Confirm` to `github_grant_proposals.consume` followed by the manager and repo checks in `github_connect._github_connect_impl`. `Revoke` represents a manager permission change before the second call. The production lifetime is 15 minutes (`github_grant_proposals._LIFETIME`); the bound keeps the same before/after-expiry ordering.

`GrantProposalUnsafe` allows `confirmed=true` to grant directly and must violate `ValidGrant`. `GrantProposalSafe` requires the later human yes, same actor, unexpired proposal, and current permission. The real-code tests in `test_github_connect.py` replay same-turn confirmation, another requester, expiry, later-turn yes, and a foreign scoped repo. `github_remove_repo.remove_repo_impl` uses the same proposal transitions with ability `remove`; `test_github_remove_repo.py` replays those confirmation cases and verifies that removal changes only the selected agent's grant, draft, scoped authorization, and working repo. The model's `granted` flag also represents completed removal because its property concerns authorization to execute the change.

The model treats a yes at turn start and grant consumption as separate actions. Its simplified permission flag does not model individual channel rules or repository rows; the integration tests cover those checks. No staging trace has been replayed.

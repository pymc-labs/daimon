------------------------ MODULE IssuedTokensPrune ------------------------
\* Can pruning delete a github_issued_tokens row that a concurrent mint, store,
\* deliver or revoke still needs, or lose track of a live GitHub token?
\* One token, one minting process. Each action is one database transaction or
\* one GitHub call, the boundaries at which the real code can be interleaved.
EXTENDS Naturals

CONSTANTS
    GraceHour,          \* TRUE: pending rows are pruned 1 h after expires_at (code). FALSE: at expires_at.
    FilterStatus,       \* TRUE: only revoked and pending rows are candidates (code).
    StoreStallBounded,  \* TRUE: a minted token is stored within an hour of the mint.
    LateRevokeCanFail,  \* TRUE: the DELETE of a late token at GitHub may fail (5xx).
    SplitPrune          \* TRUE: select candidates, then delete by id with no recheck (rejected design).

VARIABLES
    pc,         \* the minting process
    row,        \* the issued-token row
    age,        \* where the row is relative to its expires_at, see Ages
    gh,         \* the GitHub installation token
    delStatus,  \* the row's status when it was deleted
    cand        \* SplitPrune only: a candidate was selected
vars == <<pc, row, age, gh, delStatus, cand>>

\* age 0: before expires_at (the row is inserted 55 min before it)
\* age 1: past expires_at, within PENDING_GRACE
\* age 2: past PENDING_GRACE (1 h), within REVOKED_RETENTION
\* age 3: past REVOKED_RETENTION (7 d)
Ages == 0..3
Statuses == {"none", "pending", "stored", "delivered", "revoked", "deleted"}
Existing == {"pending", "stored", "delivered", "revoked"}
Tracked(r) == r \in {"stored", "delivered"}

TypeOK ==
    /\ pc \in {"idle", "created", "minted", "storeFailed", "crashed", "done"}
    /\ row \in Statuses
    /\ age \in Ages
    /\ gh \in {"none", "live", "dead"}
    /\ delStatus \in Statuses
    /\ cand \in BOOLEAN

Init ==
    /\ pc = "idle" /\ row = "none" /\ age = 0 /\ gh = "none"
    /\ delStatus = "none" /\ cand = FALSE

\* create_pending inserts the row in its own transaction (github_app_session.py:430).
Create ==
    /\ pc = "idle"
    /\ pc' = "created" /\ row' = "pending"
    /\ UNCHANGED <<age, gh, delStatus, cand>>

\* mint_installation_token returns a token (github_app_session.py:439).
MintOK ==
    /\ pc = "created"
    /\ pc' = "minted" /\ gh' = "live"
    /\ UNCHANGED <<row, age, delStatus, cand>>

\* GitHub refuses with 403, 404 or 422; the row is closed (github_app_session.py:459).
MintRefused ==
    /\ pc = "created" /\ row = "pending"
    /\ pc' = "done" /\ row' = "revoked"
    /\ UNCHANGED <<age, gh, delStatus, cand>>

\* The process dies before it stores the token. The token stays live and
\* unrecorded until it expires, with or without the prune; a pending row holds no token.
Crash ==
    /\ pc \in {"created", "minted"}
    /\ pc' = "crashed"
    /\ UNCHANGED <<row, age, gh, delStatus, cand>>

\* store_token takes the row lock and moves a pending row to stored
\* (github_issued_tokens.py:139, called at github_app_session.py:478).
StoreOK ==
    /\ pc = "minted" /\ row = "pending"
    /\ pc' = "done" /\ row' = "stored"
    /\ UNCHANGED <<age, gh, delStatus, cand>>

\* store_token raises GitHubTokenRowClosedError for a revoked row and ValueError
\* for a missing one (github_issued_tokens.py:144-148).
StoreMiss ==
    /\ pc = "minted" /\ row \in {"revoked", "deleted"}
    /\ pc' = "storeFailed"
    /\ UNCHANGED <<row, age, gh, delStatus, cand>>

\* The except handler deletes the late token at GitHub, then re-raises
\* (github_app_session.py:501-505). revoke_token raises on a 5xx, which leaves
\* the token live until it expires.
RevokeLate ==
    /\ pc = "storeFailed"
    /\ pc' = "done"
    /\ \/ gh' = IF gh = "live" THEN "dead" ELSE gh
       \/ LateRevokeCanFail /\ gh' = gh
    /\ UNCHANGED <<row, age, delStatus, cand>>

\* mark_delivered (github_issued_tokens.py:154, called at github_app_session.py:712).
Deliver ==
    /\ row = "stored"
    /\ row' = "delivered"
    /\ UNCHANGED <<pc, age, gh, delStatus, cand>>

\* The sweep deletes a stored or delivered token at GitHub (scheduler/main.py:703) ...
RevokeAtGitHub ==
    /\ Tracked(row) /\ gh = "live"
    /\ gh' = "dead"
    /\ UNCHANGED <<pc, row, age, delStatus, cand>>

\* ... and closes the row only when the token is gone, either revoked above or
\* expired (recheck_token_revoke_due, naturally_expired; scheduler/main.py:714).
MarkRevoked ==
    /\ Tracked(row) /\ gh # "live"
    /\ row' = "revoked"
    /\ UNCHANGED <<pc, age, gh, delStatus, cand>>

\* GitHub expires the token one hour after the mint. The mint is within the
\* row's first 55 minutes (see Tick), so this cannot happen at age 0.
GitHubExpiry ==
    /\ gh = "live" /\ age >= 1
    /\ gh' = "dead"
    /\ UNCHANGED <<pc, row, age, delStatus, cand>>

\* Time passes, starting at the insert. The guards are the timing facts the
\* prune depends on; the README cites each one.
Tick ==
    /\ row # "none" /\ age < 3
    /\ \/ age = 0 /\ pc # "created"
       \/ age = 1 /\ gh # "live" /\ (StoreStallBounded => pc \notin {"minted", "storeFailed"})
       \/ age = 2
    /\ age' = age + 1
    /\ UNCHANGED <<pc, row, gh, delStatus, cand>>

PendingAge == IF GraceHour THEN 2 ELSE 1

\* prune_closed_tokens' predicate on the current row version.
Eligible ==
    IF FilterStatus
    THEN \/ row = "revoked" /\ age >= 3
         \/ row = "pending" /\ age >= PendingAge
    ELSE row \in Existing /\ age >= PendingAge

\* One DELETE statement; FOR UPDATE SKIP LOCKED holds every row it deletes, so
\* a concurrent store, deliver or revoke is ordered before or after it.
Prune ==
    /\ ~SplitPrune /\ Eligible
    /\ row' = "deleted" /\ delStatus' = row
    /\ UNCHANGED <<pc, age, gh, cand>>

\* The rejected two-statement design: pick ids, then delete them by id.
SelectCandidate ==
    /\ SplitPrune /\ Eligible /\ ~cand
    /\ cand' = TRUE
    /\ UNCHANGED <<pc, row, age, gh, delStatus>>

DeleteCandidate ==
    /\ SplitPrune /\ cand /\ row \in Existing
    /\ row' = "deleted" /\ delStatus' = row /\ cand' = FALSE
    /\ UNCHANGED <<pc, age, gh>>

Next ==
    \/ Create \/ MintOK \/ MintRefused \/ Crash \/ StoreOK \/ StoreMiss \/ RevokeLate
    \/ Deliver \/ RevokeAtGitHub \/ MarkRevoked \/ GitHubExpiry \/ Tick
    \/ Prune \/ SelectCandidate \/ DeleteCandidate

Spec == Init /\ [][Next]_vars

\* A stored or delivered row is never deleted (the retention policy).
NeverDeleteLiveRow == delStatus \notin {"stored", "delivered"}

\* Once the minter has finished, a live GitHub token is recorded in a stored or
\* delivered row. Crash is excluded: that loss does not depend on the prune.
NoLiveTokenLost == (pc = "done" /\ gh = "live") => Tracked(row)

\* A store never finds its row gone. The failure is safe (RevokeLate), but it
\* fails the session that minted the token.
StoreNeverMissesRow == pc # "storeFailed"

\* Witnesses: each is meant to be violated, to show the prune is reachable.
NoPendingPruned == delStatus # "pending"
NoRevokedPruned == delStatus # "revoked"
=======================================================================

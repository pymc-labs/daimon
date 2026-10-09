# GitHub Connect cancellation

Cancel must commit a row-locked flow transition before calling GitHub to revoke
the user token. Once canceled, the same flow cannot confirm repositories. If
GitHub refuses revocation, the encrypted token stays in the flow for a later
Cancel retry and the expiry sweep leaves it alone.

| Model action | Code and executable check |
| --- | --- |
| `Cancel` | [`cancel_flow`](../../packages/core/daimon/core/stores/github_connect.py#L963) locks the flow and commits `cancelled_at` before the [route](../../packages/adapters/mcp/daimon/adapters/mcp/oauth_github.py#L789) calls GitHub. The [race test](../../packages/core/tests/test_github_connect.py#L602) replays the competing transaction. |
| `Confirm` | [`confirm`](../../packages/core/daimon/core/stores/github_connect.py#L994) takes the same row lock and rejects `cancelled_at`. The [route regression](../../packages/adapters/mcp/tests/test_oauth_github_connect.py#L166) also checks confirmation after Cancel. |
| `SiblingConfirm` | A different flow can confirm the invitation; the store's sibling update excludes canceled flows so their pending tokens survive. The route regression confirms a sibling after a failed revoke. |
| `RevokeFails`, `RevokeSucceeds` | [`revoke_user_token`](../../packages/adapters/mcp/daimon/adapters/mcp/oauth_github.py#L597) returns failure or success; only success calls [`finish_cancel_revocation`](../../packages/core/daimon/core/stores/github_connect.py#L978). The route regression covers both outcomes. |
| `Expire`, `Sweep` | [`delete_expired_flows`](../../packages/core/daimon/core/stores/github_connect.py#L913) skips canceled rows that still hold an encrypted token. The route regression expires a failed flow and retries Cancel. |

The bound is one canceled flow, one sibling confirmation, one token, and one cancellation. Each
database transition is atomic because both store paths lock the same row. The
model abstracts the network call as a success or failure and does not assume
GitHub retries or eventual recovery. Its cancellation guard excludes a flow
already expired or deleted; a previously canceled flow can still retry after
expiry. Only a successful GitHub response clears the token reference.

| Configuration | Distinct states | Verdict |
| --- | ---: | --- |
| `ConnectCancelSafe` | 20 | Clean: confirmation and lost pending revocation are excluded. |
| `ConnectCancelNoLock` | 5 | `NoConfirmAfterCancel`: Cancel returns while the flow stays confirmable. |
| `ConnectCancelDropsToken` | 16 | `PendingTokenRetained`: expiry deletes a canceled token after a failed revoke. |
| `ConnectCancelSiblingClear` | 5 | `PendingTokenRetained`: sibling confirmation clears the canceled token. |

All three counterexamples are replayed by PostgreSQL and mocked GitHub tests. The
model is finite and does not prove the real services. No live GitHub contract
test or production trace was used.

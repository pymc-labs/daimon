# Shared GitHub request card ordering

This model asks whether concurrent deliveries can post two cards for one request or exceed three admin mention posts in a thread's hour. Four deliveries cover two requests, with two retries each. `InitialMentions = 2` leaves one mention slot. The safe configuration serializes deliveries in the thread; the unsafe one removes that lock.

| Model action | Code boundary |
| --- | --- |
| Start | `request_github_access_impl` calls `deliver_shared_admin_card` in `packages/adapters/mcp/daimon/adapters/mcp/tools/github_requests.py:258` |
| Acquire | `lock_shared_card_slot` in `packages/core/daimon/core/stores/github_access_requests.py:426`; PostgreSQL advisory transaction lock |
| Check | `lookup_request` and `shared_card_mention_allowed` in `packages/adapters/mcp/daimon/adapters/mcp/tools/github_request_delivery.py:83` and `packages/core/daimon/core/stores/github_access_requests.py:434` |
| Post | `channel.send` or `chat_postMessage` in `packages/adapters/mcp/daimon/adapters/mcp/tools/github_request_delivery.py:122` and `:161` |
| Commit | `record_shared_card` in `packages/core/daimon/core/stores/github_access_requests.py:447` and transaction exit in `deliver_shared_admin_card` |

The production cap is three mention cards per thread in one hour (`shared_card_mention_allowed`). The model checks one hour; it does not model clock advancement. It starts with two mentions to exercise the last slot. The code holds the advisory lock across platform I/O and commit. The model excludes process crashes and transaction failures after a platform post: external APIs provide no atomic commit with PostgreSQL. Platform post success and edit semantics remain upstream assumptions; the unit tests replay normal duplicate delivery and verify the public payload has no repo name.

| Config | Verdict | Distinct states |
| --- | --- | ---: |
| `RequestCardsSafe` | clean | 568 |
| `RequestCardsNoLock` | violates `NoDuplicate` | 334 |

Without the lock, one delivery reads “no card” and posts, but has not committed its message ID. A second delivery reads the same absence and posts another card. The normal-delivery integration test calls the real delivery function twice and observes one post and one edit. The model result is bounded evidence for these four deliveries, not a proof against platform or process failures.

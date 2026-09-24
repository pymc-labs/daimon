# Slack first-card cancel registration

Run `formal/check.sh` from the repository root. The unsafe configuration
captures the former order: `PublishCard` can make the Cancel button visible,
`AuthorClicksVisibleCard` arrives before `ReturnPostResponse`, and the status-ts
registry has no entry yet. The safe configuration registers a turn-scoped
action value before `chat.postMessage`; the listener resolves that value while
the post response is pending. After the response, the message ts is also
registered for recovery and legacy/adopted cards. The same cancel registry
entry carries the event and author, so the existing actor check still applies.

| Model action | Source action |
| --- | --- |
| `Register` | `SlackTurnLifecycle._maybe_flush` registers the per-turn cancel key before awaiting `chat_postMessage` |
| `PublishCard` | Slack makes the `chat.postMessage` card visible, independently of returning its response |
| `ReturnPostResponse` | `chat_postMessage` returns its message ts; lifecycle registers that ts as a recovery/legacy lookup key |
| `AuthorClicksVisibleCard` | `SlackApp._handle_block_action` resolves the action value, checks the original author, and sets the turn's cancel event |

The unsafe TLC counterexample is `PublishCard` → `AuthorClicksVisibleCard`:
the user has clicked, but no registration exists, so cancellation is lost.
The fixed model requires registration before publication. Its finite state
space checks the one-turn interleaving; it does not model Slack delivery
retries, process failure, or the Python/SDK implementation.

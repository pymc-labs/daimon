These fixtures contain executable statements extracted with `ast.unparse` from
`pymc-labs/daimon` at `f324bb649a52500fe1c8928412394cb193850be4`. Comments and docstrings
are omitted. Each file identifies its original functions below. Tests execute
these functions alongside the refactored adapters with the same recorded inputs.

- `discord_card.txt`: `packages/adapters/discord/daimon/adapters/discord/embed.py`, `TurnPhase`, `EmbedEvent`, `EmbedState`, `update`, `update_activity`, and the card renderers
- `slack_card.txt`: `packages/adapters/slack/daimon/adapters/slack/blockkit.py`, `TurnPhase`, `EmbedEvent`, `State`, `update`, `update_activity`, and the card renderers
- `discord_boot.txt`: `packages/adapters/discord/daimon/adapters/discord/bot.py`, `DaimonBot._retire_orphaned_turns_once`
- `slack_boot.txt`: `packages/adapters/slack/daimon/adapters/slack/boot_sweep.py`, `retire_orphaned_turns`, `recover_slack_card_intents`
- `discord_intent.txt`: `packages/adapters/discord/daimon/adapters/discord/turn_card_recovery.py`, `reconcile_turn_card_intent`
- `teams_settle.txt`: `packages/adapters/teams/daimon/adapters/teams/app.py`, `TeamsApp._settle`

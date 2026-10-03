These are the three dispatchers from main ed54d73f2dfc594c8cc541d9f03dd324d8160389.
Paths: packages/core/daimon/core/continuity/dispatch.py and
packages/adapters/{discord,slack}/daimon/adapters/{discord,slack}/continuation_dispatch.py.

They were parsed with Python ast, had docstrings removed, and were written with
ast.unparse. Executable statements are unchanged. The equivalence test executes
these files against the real Postgres queue store and compares transition writes,
leases, retries, decisions and delivery targets with the shared dispatcher.

The adapters keep their existing notice-before-settle order, active-turn source,
full-decision versus seed runner contract, and history/clock read order. Teams
keeps core's settle-before-notice order. Changing notice order deliberately
fails eight equivalence cases. The survey's suggested notice-order and Slack
active-turn fixes are deferred because this change preserves behavior.

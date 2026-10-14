# Offline catalog tapes

`TARGET-53.txt` copies the frozen catalog identity set (SHA256
`fc14eab684b6aa257ca5c01ab113ef154c9847938d263f2739480299c43cc2f4`).
`catalog_anthropic.json` contains authored SDK records for five scenarios and
six turns. These are protocol test fixtures, with original catalog source hashes
and exact request text. They are not live recordings or model quality evidence.
The executor refuses source/request drift and verifies the prepared fixture
session uses Haiku 5.5 through the actual SDK/mux boundary.

The tool-only tape includes native bash `true` use/result records. No local
command is executed. The two-turn file scenario intentionally leaves artifact
creation/delivery unbound; it proves host session reuse only. The markdown table
tape retains raw table text rather than simulating a Discord renderer. Platform,
artifact and semantic checks earn no credit from these canned responses.

Change a source hash only with a reviewed catalog change and a matching authored
fixture. Never copy credentials or provider traffic into these files.

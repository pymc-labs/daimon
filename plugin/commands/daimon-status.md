---
description: Show which daimon servers are connected and which daimons are reachable.
---

For each connected MCP server whose name starts with `daimon-`, call `list_daimons`.
Print one table with columns: Server, Workspace, Daimon, Role. If a server is not
authenticated, print a row saying so and tell the user to run `/mcp`. If a server returns
no daimons, print a row saying daimon is not installed in any workspace they belong to.
Do not ask any daimon a question.

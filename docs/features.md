# Features

Every feature is set at one or more levels: deployment, server, agent, channel and
thread. Each level is On, Off or Default, or one of the feature's values. Default
inherits from the level above. Settings show which level decided.

An admin can lock a value at the deployment, server, agent or channel level. Levels
below then cannot change it. `disabled` means Off and locked.

Set the deployment level with `DAIMON_FEATURES__<KEY>`, for example
`DAIMON_FEATURES__PROGRESS_PLANS=on:locked`. Set the others with `/settings` in chat,
the `change_setting` MCP tool or the web app.

## Approvals

Ask before an agent writes to a third-party service. Kept in the access policy and decided most specific first, as before the registry: only a native platform admin, checked live, may change it, and it applies only once the operator enables approval overrides.

| | |
| --- | --- |
| Key | `approvals` |
| Values | `on` (Ask), `off` (Allow writes) |
| Default | `on` |
| Levels | Server, Channel, Thread |
| Combines | Most specific wins |

## Thread participation

Reply in a thread without being mentioned when a short classifier call says the agent can help. A lower tier may turn it on under a higher Off, as before the registry; a Lock stops that.

| | |
| --- | --- |
| Key | `participation` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_THREAD_PARTICIPATION__MODE` |

## Progress plans

The agent keeps a short plan of a longer task's steps on the working card, with one-line updates on what it found and does next.

| | |
| --- | --- |
| Key | `progress_plans` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_PROGRESS_PLANS__MODE` |

## Cost footer

Finished answers end with a footer naming the agent, the time taken, what the answer cost and the money left.

| | |
| --- | --- |
| Key | `cost_footer` |
| Values | `shown` (Shown), `hidden` (Hidden) |
| Default | `shown` |
| Levels | Server, Channel, Thread |
| Combines | Most specific wins |

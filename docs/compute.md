# Fly compute sessions

A person can launch a temporary Fly machine from a chat conversation and run
commands on it. Read this to turn the feature on and keep its cost bounded. It
is off by default.

## Turn it on

Set these in the MCP service **and the scheduler**:

- [`DAIMON_COMPUTE__ENABLED`](configuration.md#daimon_compute__enabled)
- [`DAIMON_COMPUTE__DEPLOYMENT_ID`](configuration.md#daimon_compute__deployment_id),
  stable and unique to this installation
- [`DAIMON_COMPUTE__ORG_SLUG`](configuration.md#daimon_compute__org_slug) and
  [`DAIMON_COMPUTE__API_TOKEN`](configuration.md#daimon_compute__api_token)
- [`DAIMON_COMPUTE__SESSION_AUTH_KEY`](configuration.md#daimon_compute__session_auth_key)
- [`DAIMON_COMPUTE__PROFILES`](configuration.md#daimon_compute__profiles), the
  image profiles people may launch

Keep the deployment id and auth key the same across restarts, and give each
installation its own Fly organisation or token scope.

> **Warning:** Removing these settings before cleanup finishes can leave paid
> machines running. To stop new launches, turn off `DAIMON_COMPUTE__ENABLED`
> and leave the rest: cleanup keeps going.

A profile names an image, regions, CPU kind, CPUs and memory:

```json
{"mmm": {"image": "registry.example/mmm@sha256:REPLACE", "regions": ["iad"],
         "cpu_kind": "shared", "cpus": 2, "memory_mb": 4096}}
```

Use immutable digests and images with no baked-in credentials. The image must
run the authenticated thin MCP server on port 8080 with `run_bash`,
`write_file`, `read_file` and `list_files`. Background commands also need
`sh`, `setsid`, `timeout`, `base64`, `tail` and a writable `/tmp`.

## How sessions work

The owner launches, extends and uses a session in the conversation it started
in. Launching and extending need an approval card. Each machine gets only its
own token and private network; Fly credentials stay in the control plane.

Reads do not renew a session's lease; commands and writes do. The scheduler
warns before expiry, deletes expired machines and retries until Fly confirms.
Keep the scheduler running while any session remains. A Discord server admin
can emergency-stop a session without seeing its files.

Inline bash stops after 40 seconds. Longer commands run in the background, and
the scheduler posts one message in the conversation when each ends. A machine
restart loses running commands, which are reported as stopped and never rerun.

## Bound the cost

There is no dollar cap or usage metering for compute.
[`DAIMON_COMPUTE__PER_PERSON_SESSIONS`](configuration.md#daimon_compute__per_person_sessions),
[`DAIMON_COMPUTE__DEFAULT_IDLE_TTL_HOURS`](configuration.md#daimon_compute__default_idle_ttl_hours),
[`DAIMON_COMPUTE__MAX_TTL_HOURS`](configuration.md#daimon_compute__max_ttl_hours)
and
[`DAIMON_COMPUTE__PER_SESSION_TASKS`](configuration.md#daimon_compute__per_session_tasks)
limit sessions, lifetimes and concurrent commands.

An active session can renew again and again, and a Fly outage can delay
deletion. Session disks do not survive, so export results before a session
stops. Erasing an account removes its sessions, and the scheduler deletes any
machine left behind.

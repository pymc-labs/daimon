# Compute sessions

A person can launch a temporary cloud machine from a chat conversation and run
commands on it. Each machine is a private Google Cloud Run service. Read this
to turn the feature on and keep its cost bounded. It is off by default.

## Turn it on

Set these in the MCP service **and the scheduler**:

- [`DAIMON_COMPUTE__ENABLED`](configuration.md#daimon_compute__enabled)
- [`DAIMON_COMPUTE__DEPLOYMENT_ID`](configuration.md#daimon_compute__deployment_id),
  stable and unique to this installation
- [`DAIMON_COMPUTE__SESSION_AUTH_KEY`](configuration.md#daimon_compute__session_auth_key)
- [`DAIMON_COMPUTE__CLOUD_RUN_PROJECT`](configuration.md#daimon_compute__cloud_run_project)
  and
  [`DAIMON_COMPUTE__CLOUD_RUN_SERVICE_ACCOUNT`](configuration.md#daimon_compute__cloud_run_service_account)
- [`DAIMON_COMPUTE__PROFILES`](configuration.md#daimon_compute__profiles), the
  image profiles people may launch

Keep the deployment id and auth key the same across restarts.

Cloud computers stay off after this set-up. A server admin then turns on the
[`compute` feature](features.md#cloud-computers) for a workspace or one agent,
with `/settings` in chat, the web app or `daimon features`. To turn it on
everywhere, set `DAIMON_FEATURES__COMPUTE=on` on every Daimon service.

A value already set for a workspace, agent, channel or thread keeps it, so an
Off there stays off, and only a server admin can change it. Turning the feature
off does not stop a machine that is running: stop it, or it ends at its idle
time.

> **Warning:** Removing these settings before cleanup finishes can leave paid
> machines running. To stop new launches, turn off `DAIMON_COMPUTE__ENABLED`
> and leave the rest: cleanup keeps going.

A profile names an image, regions, CPUs and memory:

```json
{"mmm": {"image": "us-central1-docker.pkg.dev/PROJECT/REPO/mmm@sha256:REPLACE",
         "regions": ["us-central1"], "cpus": 2, "memory_mb": 4096}}
```

A profile needs at least one Google Cloud region, and 1, 2, 4, 6 or 8 CPUs. A
launch that names no region uses the first one. Memory is 512 to 32768 MB,
within the range Cloud Run allows for that CPU count. Keep images in Artifact
Registry or on Docker Hub. A profile can also carry a `description` of up to
1,000 characters, which the agent reads: what the image holds and how to use it.

Use immutable digests and images with no baked-in credentials. Build the image
from `apps/compute-session` in the Daimon repository. An image built another
way must run the same authenticated thin MCP server on port 8080. It also
needs `bash`, `sh`, `setsid`, `nohup`, `timeout`, `base64`, `tail`, `wc`,
`tr`, a writable `/tmp` and a process 1 that reaps ended processes.

## Give Daimon access to Google Cloud

Daimon signs in to Google with application default credentials: the service
account of the machine it runs on, or the key file that
`GOOGLE_APPLICATION_CREDENTIALS` names. Grant the account of the MCP service
and of the scheduler these roles:

- Cloud Run Developer (`roles/run.developer`) on the project
- Service Account User (`roles/iam.serviceAccountUser`) on the session service
  account
- Artifact Registry Reader (`roles/artifactregistry.reader`) on the image
  repository

Cloud Run Developer on a project also covers every other Cloud Run service in
it, so use a Google Cloud project that holds only sessions when you can. Keep
that project's access small. Anyone who can read its Cloud Run services can
read a session's token. Anyone who can also call them can run commands in the
session.

Give the session service account no roles, because commands in a session can
use whatever it holds. The Cloud Run service agent of the session project
pulls the image. For an image repository in another project, grant that
service agent Artifact Registry Reader on the repository.

## How sessions work

The owner extends and uses a session only in the conversation it started in, and
can list and stop it from any of their conversations. Launching and extending
need an approval card, unless a server admin turned off the [card for cloud
computers](features.md#approval-for-cloud-computers); then only the default
lifetime runs. On OpenAI and Gemini agents the card still shows.

A session is usable once its machine has started; until then a command gets a
short notice to try again. A machine that fails to start, or has not started
after 10 minutes, is removed, and its owner is told to ask an admin.

Each machine gets only its own token, and Google credentials stay in the
control plane. The Cloud Run service refuses callers that Google IAM does not
allow.

Reads do not renew a session's lease; commands and writes do. The scheduler
warns before expiry, deletes expired machines and retries until Google
confirms. Keep the scheduler running while any session remains. A Discord
server admin can emergency-stop a session without seeing its files.

Daimon waits up to 40 seconds for a command's result. A command that needs
longer keeps running, and the scheduler posts one message in the conversation
when it ends, unless the conversation already read the result or stopped the
machine. In a thread, Daimon can read the command's output and the machine's
files for that message. Outside a thread it reports only the end of the output.

For that message Daimon cannot run commands or change the machine until the
owner writes again. A server admin can turn on [Cloud computers continue after
a job](features.md#cloud-computers-continue-after-a-job). Then, in a thread, it
may run the next command, write files and stop that machine. It never starts
or extends a machine. After
[`DAIMON_COMPUTE__WAKE_RUN_LIMIT`](configuration.md#daimon_compute__wake_run_limit)
such messages in a row it waits until the owner runs a command or writes a file
on that machine; a chat message alone does not reset it.

Google can restart a machine at any time. A restart clears its files and ends
running commands. Each is reported as stopped, with the last output Daimon
saved, and is never rerun.

## Bound the cost

There is no dollar cap or usage metering for compute.
[`DAIMON_COMPUTE__PER_PERSON_SESSIONS`](configuration.md#daimon_compute__per_person_sessions),
[`DAIMON_COMPUTE__DEFAULT_IDLE_TTL_HOURS`](configuration.md#daimon_compute__default_idle_ttl_hours),
[`DAIMON_COMPUTE__MAX_TTL_HOURS`](configuration.md#daimon_compute__max_ttl_hours)
and
[`DAIMON_COMPUTE__PER_SESSION_TASKS`](configuration.md#daimon_compute__per_session_tasks)
limit sessions, lifetimes and concurrent commands.

Each session keeps one instance running with its CPU always on, so it costs
money for its whole life. Files a session writes use its memory. An active
session can renew again and again, and a Google outage can delay deletion.

Session files do not survive, so export results before a session stops.
Erasing an account removes its sessions, and the scheduler deletes any machine
left behind. It looks for leftover machines only in regions that a profile
names, so keep a region listed until its sessions are gone.

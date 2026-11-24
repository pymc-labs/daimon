# Compute sessions

A person can launch a temporary cloud machine from a chat conversation and run
commands on it. Read this to turn the feature on and keep its cost bounded. It
is off by default.

## Turn it on

Set these in the MCP service **and the scheduler**:

- [`DAIMON_COMPUTE__ENABLED`](configuration.md#daimon_compute__enabled)
- [`DAIMON_COMPUTE__DEPLOYMENT_ID`](configuration.md#daimon_compute__deployment_id),
  stable and unique to this installation
- [`DAIMON_COMPUTE__SESSION_AUTH_KEY`](configuration.md#daimon_compute__session_auth_key)
- [`DAIMON_COMPUTE__PROFILES`](configuration.md#daimon_compute__profiles), the
  image profiles people may launch
- the settings of one [provider](#choose-a-provider)

Keep the deployment id and auth key the same across restarts.

> **Warning:** Removing these settings before cleanup finishes can leave paid
> machines running. To stop new launches, turn off `DAIMON_COMPUTE__ENABLED`
> and leave the rest: cleanup keeps going.

A profile names an image, regions, CPU kind, CPUs and memory:

```json
{"mmm": {"image": "registry.example/mmm@sha256:REPLACE", "regions": ["iad"],
         "cpu_kind": "shared", "cpus": 2, "memory_mb": 4096}}
```

Use immutable digests and images with no baked-in credentials. Build the image
from `apps/compute-session` in the Daimon repository. An image built another
way must run the same authenticated thin MCP server on port 8080. It also
needs `bash`, `sh`, `setsid`, `nohup`, `timeout`, `base64`, `tail`, `wc`,
`tr`, a writable `/tmp` and a process 1 that reaps ended processes.

## Choose a provider

[`DAIMON_COMPUTE__PROVIDER`](configuration.md#daimon_compute__provider) selects
where machines run. Stop every session before you change it, because Daimon
cleans up only the selected provider.

| Provider | Value | A session is | Also set |
| --- | --- | --- | --- |
| Fly | `fly`, the default | one Fly app with one machine | [`DAIMON_COMPUTE__ORG_SLUG`](configuration.md#daimon_compute__org_slug), [`DAIMON_COMPUTE__API_TOKEN`](configuration.md#daimon_compute__api_token) |
| Google Cloud Run | `cloud_run` | one private service with one instance | [`DAIMON_COMPUTE__CLOUD_RUN_PROJECT`](configuration.md#daimon_compute__cloud_run_project), [`DAIMON_COMPUTE__CLOUD_RUN_SERVICE_ACCOUNT`](configuration.md#daimon_compute__cloud_run_service_account) |

On Fly, give each installation its own Fly organisation or token scope.

### Cloud Run

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

A profile needs Google Cloud regions such as `us-central1`, and 1, 2, 4, 6 or
8 CPUs. Memory is 512 to 32768 MB, within the range Cloud Run allows for that
CPU count. Cloud Run ignores `cpu_kind`. Keep images in Artifact Registry or
on Docker Hub.

Each session keeps one instance running with its CPU always on, so it costs
money for its whole life. Files a session writes use its memory. Google can
restart the instance at any time, which ends running commands and clears its
files. The scheduler looks for leftover sessions only in regions that a
profile names, so keep a region listed until its sessions are gone.

## How sessions work

The owner launches, extends and uses a session in the conversation it started
in. Launching and extending need an approval card. Each machine gets only its
own token, and provider credentials stay in the control plane. A Fly machine
has a private network; a Cloud Run service refuses callers that Google IAM
does not allow.

Reads do not renew a session's lease; commands and writes do. The scheduler
warns before expiry, deletes expired machines and retries until the provider
confirms. Keep the scheduler running while any session remains. A Discord
server admin can emergency-stop a session without seeing its files.

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

An active session can renew again and again, and a provider outage can delay
deletion. Session disks do not survive, so export results before a session
stops. Erasing an account removes its sessions, and the scheduler deletes any
machine left behind.

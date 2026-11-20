# Evals

Evals check how well an agent handles real work. A suite holds cases: a
conversation, the world the agent can reach, and what a good answer does.
Suites and their results travel between deployments as bundles.

## Turn it on

Evals are off by default. Turn on the `evals` feature for the deployment or a
workspace, as in [Features](features.md). Off hides the eval tools and refuses
every eval request.

To offer the evals section of the [web app](web.md), add `evals` to
[`DAIMON_WEB__MODULES`](configuration.md#daimon_web__modules).

## Suites and cases

A suite has an id, a name and the cases it pins. Each change to a suite saves
a new version, so results always name the version they ran.

A case is one JSON document in the bundle format. Saving a case again adds a
revision; saving the suite pins it in a new version. Its hash covers every field, so
any edit shows. Files a case uses come into the suite with a bundle import.

Check a case before you save it:

```bash
daimon evals cases validate case.json
```

## Cases from threads

Make cases from a conversation you can read. Each turn the agent answered becomes
one case: the messages before it are history, and the world holds only what that
turn used. Send a Discord, Slack or Teams link to the `eval_case_from_thread` tool,
a web chat link to `POST /api/evals/suites/{id}/cases/from-thread`, or a web chat
link or a saved thread file to the command line:

```bash
daimon evals cases from-thread support-basics https://example.com/chat/<id>
daimon evals cases from-thread support-basics --record thread.json
```

Chat platforms keep messages, not tool calls, so these cases have `text` fidelity
and the simulator fills the gaps. A case starts with the recorded answer as its
reference and an `expectations_unreviewed` flag.

Draft the pass criteria with a model, then review them. The command shows a cost
estimate first and drafts only with `--yes`:

```bash
daimon evals cases draft support-basics <case-id> --yes
```

Repeated requests are marked as near-duplicates with `daimon evals cases dedupe`.
Add `--keep 2` to keep the two oldest of each group.

## Scrubbing

Every case made from a thread is scrubbed before it is saved. Known names and those the
name model finds get the same fake everywhere: in messages, tool data, file names and
file contents. Known names are the workspace's people, channels and repos, plus the thread.
Common secret and token formats are removed. Word, Excel, PowerPoint, PDF and text files
are rebuilt with fakes and images are blanked where text is found. Other files are left out.

Set [`DAIMON_EVALS__SCRUB_KEY`](configuration.md#daimon_evals__scrub_key) first.
Install the scrub tools with `pip install "daimon-core[evals]"`, add the spaCy model
named in [`DAIMON_EVALS__SCRUB_NER_MODEL`](configuration.md#daimon_evals__scrub_ner_model)
and the `tesseract` program for images. Without the model only known names and patterns
are found, and cases stay `partial`. Only a `scrubbed` case can be shared; a person
should still read a case before it leaves the workspace.

Scrub a bundle folder, zip or encrypted file offline, with a dictionary of the names
your data holds (people, emails, companies, projects, hostnames, repos, channels, phrases):

```bash
export DAIMON_EVALS__SCRUB_KEY=<at least 16 characters>
daimon evals bundle scrub bundle/ --entities names.yaml --salt my-workspace --map fakes.json --out clean/
daimon evals bundle leftovers clean/ --entities names.yaml --salt my-workspace --map fakes.json
```

Both print counts and places, never the text they found, and exit 1 unless the bundle
is fully scrubbed. Reuse the same key, `--salt` and `--map` for every bundle of a suite
so fakes stay the same. Scrub each raw bundle once: a second scrub fakes its names again.
Add `--jobs 2` or more to run the names model in that many processes; each loads it.
The map holds only hashes and fakes, but keep it private.

To scrub many bundles one after another, pass the same `--learned names-found.json` to each
`scrub` (and to `leftovers`). It is read first, as extra dictionary entries, and the names the
model finds in a run are added to it at the end, so a name found in one bundle is replaced in
every later one. It holds real names in plain text (created mode 600): keep it private and
delete it with the raw bundles. The map and that file are saved before the bundle is
written, under a lock; run batches one at a time, not side by side.

A one-word name that is also an everyday word ("Will", "Hope", "Young") is replaced only when
it is written as in the dictionary, capitalized; the sweep cannot see the lowercase form. Add
such handles to the dictionary as they appear.

## The world a case runs in

A running case never reaches real services. Each call it makes, over HTTP, git
or an MCP tool, is answered by a fake world built from the case:

1. A response the case recorded for that request.
2. What the case's earlier calls changed, so a read sees an earlier write.
3. A simulated answer, checked against the service's API and kept for reuse.

When nothing can answer, or the case's call limits run out, the call is refused.
Every call is logged with the effects it would have had, such as a created issue.
Pushes go to a private copy of the repository for that item only. Set the
simulator with [`DAIMON_EVALS__SIMULATOR_MODEL`](configuration.md#daimon_evals__simulator_model).

## Sandboxes

Each item runs its tools in an enclosed sandbox pod. A gateway in the pod sends
every web request the tools make to the fake world and refuses every other
connection. The provider worker that runs the agent reaches its provider, and
the shells it starts run as the agent. Package downloads are blocked
unless the `eval_package_registries` feature lets them reach the real registries.

A pod gets 2 CPUs, 4 GB of memory, 4,096 processes and 20 GB of disk.
Docker has no per-container disk cap, and on Kubernetes the process limit is
the node's. Kubernetes stops a pod after 4 hours. Tools written in Go, such
as `gh`, see the real clock instead of the case's.

Limits: this encloses the agent's tools, not a hostile executor. A command the
executor starts without a shell, such as the codex backend's `python3` or `curl`,
and a direct run of the real shell binaries, run as the executor user. That user
reaches the provider's control plane and any
[`passthrough_hosts`](configuration.md#daimon_evals__passthrough_hosts) untouched,
and the gateway matches them by TLS server name, not by the request's Host header.
Provider web tools are off only for agents compiled by the eval path.

Pods run on a Docker host or in a Kubernetes namespace, chosen by
[`DAIMON_EVALS__SANDBOX_RUNTIME`](configuration.md#daimon_evals__sandbox_runtime).
The pod sets its own packet rules, so the platform must allow the `NET_ADMIN`
capability. Managed container services that forbid it cannot host sandboxes.

On Kubernetes, the eval worker's service account needs to create, read, list and
delete pods, `pods/exec` and secrets in the namespace. The namespace needs the
`privileged` pod security level. Pods get no service account token. Add a
NetworkPolicy that admits connections to pods labelled `daimon.eval/item` only
from the eval worker.

Sandbox tools trust an eval CA. Create its key once and store it as
[`DAIMON_EVALS__CA_KEY`](configuration.md#daimon_evals__ca_key):

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out eval-ca.pem
```

## Who can do what

| Person | Can |
| --- | --- |
| Workspace owner or admin | Everything, on every suite |
| Suite owner | Everything on their suite, including sharing it |
| Editor | Change cases and export scrubbed bundles |
| Reviewer | See the suite and review results |
| Viewer | See the suite |
| Visitor | Nothing |

Members create suites and own what they create. Owners share a suite with people
or groups. Group membership is checked live. When the platform cannot say, access is denied.

Shares reach only scrubbed suites, cases and runs. Content that is not scrubbed is
for owners and admins, even on a shared suite.

## Bundles

A bundle is a zip with a `bundle.json` manifest, suites, runs and blobs. The
manifest lists every file with its sha256, so accidental damage shows. Only a
signature from a trusted key proves who wrote a bundle.

Validate a bundle folder, zip or encrypted file. This works offline, with no
database:

```bash
daimon evals bundle validate path/to/bundle.zip
```

It prints a JSON report and exits with 1 on any problem. Set
`DAIMON_EVAL_PASSPHRASE` or pass `--passphrase` for an encrypted bundle.

Export and import from the command line:

```bash
daimon evals bundle export --suite support-basics --out support.zip
daimon evals bundle import support.zip
```

Import also takes a bundle folder. Export replaces people with neutral labels.
Only an admin can keep identities. Only an owner or an admin can export content
that is not scrubbed. Import refuses a bundle that is not scrubbed unless an owner
or an admin accepts it with `--accept-unscrubbed`.

Imported runs are records: unfinished ones arrive cancelled and never run here.
File contents live in the [artifact store](configuration.md#daimon_artifacts__backend)
when one is set up, otherwise in the database.

### Signatures and encryption

Set [`DAIMON_EVALS__SIGNING_KEY`](configuration.md#daimon_evals__signing_key) to sign
exported bundles. With
[`DAIMON_EVALS__TRUSTED_KEYS`](configuration.md#daimon_evals__trusted_keys) set,
import takes only bundles signed by those keys or this deployment's key. A bad
signature from a trusted key is always refused.

A passphrase encrypts the bundle with AES-256-GCM and a scrypt key. Share the
passphrase another way than the file. MCP tools never take a passphrase: export
and import encrypted bundles from the command line or the web app.

## Where to use it

The same rules apply on every surface:

- The web app, under `/api/evals`.
- MCP tools whose names start with `eval_`, listed in [MCP tools](mcp-tools.md).
- The `daimon evals` commands.

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
revision and a new suite version. Its hash covers every field, so any edit shows.

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

Every case made from a thread is scrubbed before it is saved. People, emails,
companies, projects, channels, hosts and ids get the same fake everywhere: in
messages, tool data, file names and file contents. Secrets and signed-URL tokens
are removed. Word, Excel, PowerPoint and PDF files are rebuilt with fakes, and images
are blanked where text is found and flagged for a person to check.

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
daimon evals bundle scrub bundle/ --entities names.yaml --map fakes.json --out clean/
daimon evals bundle leftovers clean/ --entities names.yaml --map fakes.json
```

Both print counts and places, never the text they found, and exit 1 when something is
left. Reuse the same key and `--map` for every bundle of a suite so fakes stay the same.
The map holds only hashes and fakes, but keep it private.
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

A suite that is not scrubbed cannot be shared.

## Bundles

A bundle is a zip with a `bundle.json` manifest, suites, runs and blobs. The
manifest lists every file with its sha256. Edits outside Daimon break validation.

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

Export replaces people with neutral labels. Only an owner or an admin can keep
identities or export content that is not scrubbed. Import refuses a bundle that is
not scrubbed unless an owner or an admin accepts it with `--accept-unscrubbed`.

### Signatures and encryption

Set [`DAIMON_EVALS__SIGNING_KEY`](configuration.md#daimon_evals__signing_key) to sign
exported bundles. Import checks signatures from the keys in
[`DAIMON_EVALS__TRUSTED_KEYS`](configuration.md#daimon_evals__trusted_keys). A bad
signature from a trusted key is refused.

A passphrase encrypts the bundle with AES-256-GCM and a scrypt key. Share the
passphrase another way than the file.

## Where to use it

The same rules apply on every surface:

- The web app, under `/api/evals`.
- MCP tools whose names start with `eval_`, listed in [MCP tools](mcp-tools.md).
- The `daimon evals` commands.

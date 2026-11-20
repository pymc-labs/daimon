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

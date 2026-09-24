# Report publish transaction

`ReportPublish.tla` follows one publish attempt for an existing report. The
initial published PDF, bundle handle, digest, and archive are coherent. The
safe configuration checks one incoming revision through archive persistence,
the seam response, PDF-file write, and the SQLite transaction that records the
revision and switches the visible report tuple.

## Source mapping

- `PersistUniqueArchive` corresponds to `publish_upload` persisting a
  jti-specific archive before calling the seam in
  [`uploads.py`](../../apps/report-host/src/report_host/uploads.py#L347).
- `SeamAccept`, `SeamReject`, and `SeamTimeout` abstract the awaited
  `push_bundle` and its error boundary in
  [`uploads.py`](../../apps/report-host/src/report_host/uploads.py#L362).
  A timeout may mean the remote service accepted the bundle before its reply
  was lost; the model permits that remote orphan while requiring local readers
  to retain the previous published tuple. A process death after acceptance and
  before the local commit has the same possible orphan outcome.
- `WriteAcceptedPDF` and `CommitAcceptedPublish` represent the PDF write after
  a successful seam response, followed by the SQLite transaction in
  [`uploads.py`](../../apps/report-host/src/report_host/uploads.py#L382) and
  [`reports_store.py`](../../apps/report-host/src/report_host/reports_store.py#L337).
  The transaction inserts the revision and changes `current_pdf`, bundle
  handle, digest, expiry, and archive path together.
- `CommitFailure` models that transaction aborting after the PDF file exists.
  The PDF may remain as an unreferenced file, while SQLite keeps the old
  revision and bundle tuple.
- `Crash` may stop the single attempt at any modeled boundary. It does not
  restart it or retry the single-use capability.

The pre-fix configuration represents the former order: write and expose the
new PDF/revision before the seam accepts the archive, then overwrite the
referenced `bundle.tar.gz`. TLC finds the shortest two-state counterexample at
the early exposure: the report points at `new.pdf` while its accepted bundle
and digest still name the old report. The model also includes the later
fixed-path overwrite, which would leave the old bundle handle pointing at new
archive bytes if the seam then failed. `VisibleArtifactsCoherent` requires the
current PDF, recorded revision, digest, bundle, and existing archive bytes to
describe one accepted publish.

## Executable calibration

- [`test_publish_archive_survives_a_seam_error`](../../apps/report-host/tests/test_uploads.py#L612)
  checks an HTTP failure and an ambiguous timeout: old PDF, handle, digest,
  and archive remain paired; the incoming archive is retained separately.
- [`test_record_published_revision_rolls_back_all_fields_on_database_error`](../../apps/report-host/tests/test_reports_store.py#L119)
  injects a SQLite update failure after the revision insert and checks the
  transaction leaves the previous report and revision list intact.
- [`test_publish_happy_path_writes_pdf_archive_and_returns_recipient_links`](../../apps/report-host/tests/test_uploads.py#L533)
  checks the successful path commits the new PDF and archive reference.

These tests exercise the implementation. TLC checks the smaller abstraction;
neither the model nor these bounded cases prove all filesystem, SQLite, or
upstream behavior.

## Bounds and exclusions

The model contains one report, one current revision and bundle, one candidate
revision, one single-use publish attempt, and one seam call. It checks safety
only; there is no fairness assumption or progress claim because rejection,
timeout, and process death may stop the attempt. A timeout or process death
after remote acceptance can leave an upstream bundle that the local database
does not reference. The model does not promise exactly-once remote effects,
retry recovery, two overlapping publish attempts, concurrent readers,
filesystem durability beyond the atomic-write boundary, or the reader-turn
upload route. Those behaviors need their own evidence before making broader
claims.

Run both configurations with the repository-wide command from the root:

```sh
formal/check.sh
```

To inspect the pre-fix trace directly, run:

```sh
cd formal/report_publish
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config PublishPreFix.cfg ReportPublish.tla
```

# Request schemas

Every request is `{"operation": "...", "body": {...}, "target": ...}`.
Omit target for creates/mail. Unknown fields and operations are refused.

| Operation | Target | Body |
| --- | --- | --- |
| `gmail-draft`, `gmail-send` | none | `to`: explicit email list, optional `cc`, `bcc` lists; `subject`, `text` strings |
| `drive-create` | none | `name`, optional `mimeType`, `description`, `parents` (one explicit folder ID) |
| `drive-copy`, `drive-rename` | existing file ID | `name` only |
| `docs-create` | none | `title` |
| `docs-edit` | existing document ID | `requests`: 1–100 Google Docs `insertText`, `replaceAllText` or `deleteContentRange` objects |
| `calendar-create` | `{"calendar_id":"..."}` | `summary`, `start`, `end`; optional `description`, `location`, `attendees` (objects containing `email` only) |
| `calendar-update` | `{"calendar_id":"...","event_id":"..."}` | any nonempty subset of the event fields above |

Calendar start/end use Google `date` or `dateTime`, optionally `timeZone`.
Calendar updates patch only named fields; create still requires summary/start/end.
Drive create/copy/rename and Slides template copy/get opt into Shared Drive
support with `supportsAllDrives=true`. Attendees may receive notifications
according to Google behavior; include only people authorized by the task.
Docs request bodies use the Google Docs API shape (no inline shell JSON).
For example, append to a document:

```json
{"operation":"docs-edit","target":"EXPLICIT_DOCUMENT_ID","body":{"requests":[{"insertText":{"endOfSegmentLocation":{},"text":"First paragraph\nSecond paragraph\n"}}]}}
```

Operator Google scopes for these workflows: `https://www.googleapis.com/auth/`
followed by `gmail.send`, `gmail.compose`, `drive`, `documents`, `calendar.events`
and (for slides-workflow) `presentations`. Full Drive scope supports existing
files shared with the bound user and templates. The broker service-account
credential stays server-side under `DAIMON_CREDENTIALS__GOOGLE_SA_JSON`.

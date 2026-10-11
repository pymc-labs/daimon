# PDF output ownership observations

`daimon.testing.output_ownership` is an opt-in offline observation binding for
D12-PDF-CLAIM-HAS-PDF. It leaves the production adapter and the frozen scenario
and provider fixtures unchanged. It never creates `TurnEvidence`.

`capture_native_artifact(artifacts, scope, ref, session=..., root_turn_id=...,
tape=FilePin(...), taped_body=FilePin(...), recording_root=...)` uses the real
provider artifact port. The native metadata must identify the selected provider,
provider account, tenant, account and session. A native turn id, when present,
must match the selected root. The download must match the actual binary body
sidecar in the pinned tape, byte for byte, within the existing delivery limit.
Both files are checked again after downloading. Gemini's driver extracts a
mounted snapshot member through its normal artifact download implementation;
the observation hook does not unpack an invented provider payload or run bash.

`OutputUploadGateway` extends the existing offline Discord HTTP endpoint. Install
it as the real discord.py thread/client HTTP endpoint before delivery.
`observe_pdf_delivery(native, thread, gateway, may_post=..., answer=...)` calls
#750's unchanged `deliver_provider_output` edge. It captures the actual
multipart file stream without changing its position, records only acknowledged
uploads, and refetches the exact accepted message and attachment to read its
bytes back. Answer edits and the adapter's fallback sends both work. A stale
receipt, an attachment name, or a method return cannot substitute for an upload.
The evidence binds the channel, answer for an edit, upload identity, filename,
and native/upload/readback SHA-256 hashes, retaining the actual bytes as base64
in its JSON-safe envelope.

`check_pdf` requires `%PDF-` magic and a real `qpdf --check` object/xref parse,
plus a nonempty page tree. The optional local `qpdf` executable never uses the
network. If it is unavailable or times out, the check is PENDING. Parser
output is not logged. A renamed image, MIME declaration, attachment claim or
magic header without a parse is not a PDF. JSON decoding rechecks the hash,
identity and PDF verdict against the captured bytes.

Delivery checks and model-generation proof are separate. Positive protocol
controls use synthetic PDFs only to test the real driver/download and adapter
upload paths. Their `generation_status` is always PENDING; no source pin or
fixture tag can promote it. A generation binding requires an independently
owned native recording association between the current root and artifact bytes.
This first slice does not supply that missing association or replace the
remaining full-cell assertions, including complete post history and the footer.

`d12_availability()` publishes three source-pinned PENDING cuts for N9. The
Anthropic authored tape has successful bash text and a canned attachment claim
but no downloaded/mounted bytes. OpenAI and Gemini's pinned native recipes mark
`ARTIFACT_PROTOCOL_UNBOUND`. The current result is **0 PASS, 0 FAIL, 3 PENDING**;
zero cells move to executed. Consumers must preserve this result until a real
artifact recording is available, rather than seed a PDF to make the cell pass.
The exact signed source commits and file hashes are retained in each row.

The current availability rows pin the exact published fixture snapshot
`5787984757acc93ac15d4e8bd6e183769d01139b`. Its native D12 digest is
`453f7cc144b5f064cdd8eb1990a3f54354ec7ec48007abb3c61c2a7662cbcbb7`;
the Anthropic replay bytes retain their earlier digest. The Gemini codec metadata
refresh changes no recipe or artifact availability. Every row remains PENDING.

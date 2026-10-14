# Offline attachment ingestion observations

`daimon.testing.attachment_ingestion` binds a real `discord.Attachment.read`
download to its original gateway message, channel, attachment ID, URL, size and
frozen catalog asset hash. The capture retains actual bytes. Revalidation after
the awaited download detects a changed source asset. JSON roundtrips retain the
identity and byte checks.

`capture_mount_control` observes the existing `FakeSessionsState` materializer
after real Anthropic driver upload and session-resource operations. It checks
the actual uploaded file bytes, accepted resource ID, session ownership, mount
index and sandbox bytes. It does not write fixture state. The boundary is
explicitly `offline_native_mount_materializer`, with `scenario_cell_credit`
fixed to false. This is an SDK protocol control, not provider workspace execution.

Discord's current PDF ingestion passes the signed CDN URL in the native user
message. The adapter does not eagerly download or upload the PDF. An observation
control that separately reads and mounts the PDF cannot establish that the agent
fetched that URL during the catalog turn. The gateway composition test uses C7's dual-signed
`a456c7ff2f99f462c3c12a990e1aefeeade62007` snapshot: it sends the original
PDF mention through the real parser and bot, observes the accepted native input,
then drives a separate download/mount control. D1 also sends the original
follow-up through the same session; this does not create `qa_canary.txt` or
prove that the native agent executed its command.

`attachment_cells(scenario)` returns the following conservative current cuts:

| Scenario | Anthropic | OpenAI | Gemini |
| --- | --- | --- | --- |
| NEW1-PDF-BARE-MENTION-NO-DROP | PENDING: native fetch/mount unbound | PENDING: ingress unbound | PENDING: ingress unbound |
| D1-CANARY-TWO-TURN | PENDING: workspace execution/readback unbound | PENDING: ingress unbound | PENDING: ingress unbound |

C7 currently excludes alternate native runtimes. D1's replay text
`FILE=a1b2c3d4e5f6` has no independent creation or readback bytes for
`qa_canary.txt`. It cannot prove the authored random-file command ran, that the
file survived the follow-up, or that the follow-up read the same workspace.
No `TurnEvidence`, native root, complete catalog PASS or generation proof is
created here. Frozen catalog sources, native fixtures and goldens are unchanged.

Offline download/mount controls still run when optional `qpdf` is absent.
The PDF parse verdict then remains `PENDING` (`PDF_PARSER_UNAVAILABLE`);
successful byte transport supplies neither PDF validation nor cell credit.

# Slack adapter

The output-delivery module exposes `deliver_provider_output` for verified neutral
provider bytes. The caller must validate the admitted tenant, provider session
and authoritative root before supplying a `DeliverableFile`. This edge does
not select outputs or fetch provider content; host/event wiring is separate.

Uploads reuse the real Slack `files_upload_v2` flow with the existing filename
handling and exact content bytes. Slack determines previews from the filename;
no unsupported `content_type` parameter is added.

# Teams adapter

The output-delivery module exposes `deliver_provider_output` for verified neutral
provider bytes. The caller must validate the admitted tenant, provider session
and authoritative root before supplying a `DeliverableFile`. This edge does
not select outputs or fetch provider content; host/event wiring is separate.

Channel uploads reuse Graph Files and append the returned file link to the
answer. Personal chats reuse consent cards and SharePoint PUT. Neutral bytes
stay server-side, with bounded retention across pending offers and in-flight
uploads, and are released on consent completion or expiry cleanup. Clicks bind
the requester, conversation and service URL. Neutral consent does not read or
delete Anthropic outputs, or delete the provider artifact.

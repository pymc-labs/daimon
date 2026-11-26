"""Chat tools for the data pages: sources, access, links and redaction rule sets.

Each read answers directly. Each change runs only on the asker's Approve card
(`CARDED_TOOLS`), in any room or direct message, through the same `daimon.data` functions and
permission checks as the web routes. Credentials never pass through chat or tool arguments: a
source that needs one gets the asker's private form (`request_data_source_credential`), whose
value goes straight onto the source.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._data_admin import (
    account_of,
    cut,
    holder_of,
    keys_of,
    manager,
    names_of,
    plain,
    plain_all,
    refused,
    schemas_of,
    store,
    uuid_of,
)
from daimon.adapters.mcp.tools._private_values import privately
from daimon.adapters.mcp.tools.credential_requests import request_data_source_credential
from daimon.data import identity, manage, redaction
from daimon.data.identity import IdentityError, IdentityKey
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

SourceAction = Literal[
    "create", "set_credential", "pause", "resume", "retire", "publish", "unpublish", "sync"
]
AccessAction = Literal["create_scope", "delete_scope", "grant", "revoke"]
LinkAction = Literal["link", "reject", "unlink", "hide_names", "show_names"]
_DONE: dict[str, Any] = {"done": True}
_NO_SECRETS = (
    "Never pass a credential here. Leave it out: the source gets a private form for it. "
    "Nothing changed."
)
_WEB_ONLY = "This source's credentials are set in the web app. Nothing changed."


def _key(provider: str, workspace: str, subject: str) -> IdentityKey:
    if not (provider and workspace and subject):
        raise ToolError("Give the `provider`, `workspace` and `subject` as listed.")
    return IdentityKey(provider, workspace, subject)


def register_data_admin_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    schemas = schemas_of(runtime)

    # ---- sources ------------------------------------------------------------------------

    @mcp.tool(name="list_data_sources", tags={"admin"})
    async def list_sources(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, source_id: str = ""
    ) -> dict[str, Any]:
        """List the workspace's data sources with their state, or one source's latest sync
        runs (source_id). The kinds of source that can be added are listed too. Admin-only."""
        actor = await manager(runtime, await _auth(ctx))
        async with store(runtime) as conn:
            try:
                if source_id:
                    found = await manage.runs(
                        conn, schemas, actor.tenant_id, uuid_of(source_id, "source_id")
                    )
                    return {"runs": [{**r, "error": cut(r["error"])} for r in plain_all(found)]}
                listed = await manage.sources(conn, schemas, actor.tenant_id)
            except IdentityError as e:
                raise ToolError(str(e)) from None
        return {
            "sources": [{**s, "last_error": cut(s["last_error"])} for s in plain_all(listed)],
            "kinds": plain_all(manage.kinds()),
        }

    @mcp.tool(name="change_data_source", tags={"admin"})
    async def change_source(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        action: SourceAction,
        origin_context_id: str,
        source_id: str = "",
        kind: str = "",
        name: str = "",
        settings: dict[str, str | list[str]] | None = None,
    ) -> dict[str, Any]:
        """Add a data source (create: kind, name, settings by the kind's non-secret fields),
        post the asker's private form for a source's credential (set_credential: source_id),
        or pause, resume, retire, publish, unpublish or sync one (source_id). Runs on the
        asker's Approve card. Never ask for a credential in chat or pass one in settings: a
        source that needs one waits, paused, for the private form this posts, and only the
        asker can fill it in. Admin-only."""
        auth = await _auth(ctx)
        actor = await manager(runtime, auth)
        await require_card(
            runtime, auth, tool_name="change_data_source", origin_context_id=origin_context_id
        )
        async with store(runtime) as conn:
            try:
                if action == "create":
                    fields = {f.name: f for k in manage.kinds() if k.kind == kind for f in k.fields}
                    secret = manage.secret_field(kind)
                    if any(f.kind == "secret" and f.name in (settings or {})
                           for f in fields.values()):  # fmt: skip
                        raise ToolError(_NO_SECRETS)
                    needs = any(f.kind == "secret" and f.required for f in fields.values())
                    if needs and secret is None:
                        raise ToolError(_WEB_ONLY)
                    made = await manage.create_source(
                        conn, schemas, keys_of(runtime), tenant_id=actor.tenant_id,
                        by=actor.account_id, kind=kind, name=name, settings=dict(settings or {}),
                        credential_later=needs,
                    )  # fmt: skip
                    if not needs:
                        return {"created": str(made)}
                    form = await request_data_source_credential(
                        runtime, auth, source_id=made, name=name.strip(),
                        origin_context_id=origin_context_id,
                    )  # fmt: skip
                    return {"created": str(made), "credential_form": form.instruction}
                if action == "set_credential":
                    source = uuid_of(source_id, "source_id")
                    found = [s for s in await manage.sources(conn, schemas, actor.tenant_id)
                             if s.id == source]  # fmt: skip
                    if not found:
                        raise ToolError("That source isn't here.")
                    if manage.secret_field(found[0].kind) is None:
                        raise ToolError(_WEB_ONLY)
                    form = await request_data_source_credential(
                        runtime, auth, source_id=source, name=found[0].name,
                        origin_context_id=origin_context_id,
                    )  # fmt: skip
                    return {"credential_form": form.instruction}
                await manage.act(
                    conn, schemas, tenant_id=actor.tenant_id, by=actor.account_id,
                    source_id=uuid_of(source_id, "source_id"), action=action,
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None
        return _DONE

    # ---- scopes and grants ----------------------------------------------------------------

    @mcp.tool(name="list_data_access", tags={"admin"})
    async def list_access(ctx: Context) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """List who may read what in the data sources: each scope with its holders, and the
        workspace's people (names and account ids). Admin-only."""
        actor = await manager(runtime, await _auth(ctx))
        names = await names_of(runtime, actor.tenant_id)
        async with store(runtime) as conn:
            scopes = await manage.scopes(conn, schemas, actor.tenant_id)

        def holder(h: manage.GrantView) -> dict[str, Any]:
            return {"holder": h.holder, "name": _holder_name(h.holder, names), "bound": h.bound}

        return {
            "scopes": [
                {"id": s.id, "kind": s.kind, "name": s.name, "description": s.description,
                 "bound": s.bound, "holders": [holder(h) for h in s.holders]}
                for s in scopes
            ],
            "people": [
                {"account_id": str(a), "name": n} for a, n in sorted(names.items(), key=_by_name)
            ],
        }  # fmt: skip

    @mcp.tool(name="change_data_access", tags={"admin"})
    async def change_access(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        action: AccessAction,
        origin_context_id: str,
        scope_id: str = "",
        holder: str = "",
        kind: str = "",
        name: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Change who may read what in the data sources. create_scope (scope_id, kind, name,
        description), delete_scope (scope_id), grant or revoke a scope (scope_id, holder: a
        person's name or account id, or a holder as listed). Runs on the asker's Approve card.
        Admin-only."""
        auth = await _auth(ctx)
        actor = await manager(runtime, auth)
        await require_card(
            runtime, auth, tool_name="change_data_access", origin_context_id=origin_context_id
        )
        names = await names_of(runtime, actor.tenant_id)
        t, by = actor.tenant_id, actor.account_id
        async with store(runtime) as conn:
            try:
                match action:
                    case "create_scope":
                        await manage.create_scope(
                            conn, schemas, tenant_id=t, by=by, admin=True, id=scope_id, kind=kind,
                            name=name, description=description,
                        )  # fmt: skip
                    case "delete_scope":
                        await manage.delete_scope(
                            conn, schemas, tenant_id=t, by=by, admin=True, id=scope_id
                        )
                    case "grant":
                        await manage.grant(
                            conn, schemas, tenant_id=t, by=by, admin=True, scope=scope_id,
                            holder=holder_of(names, holder),
                        )  # fmt: skip
                    case "revoke":
                        await manage.revoke(
                            conn, schemas, tenant_id=t, by=by, admin=True, scope=scope_id,
                            holder=holder_of(names, holder),
                        )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None
        return _DONE

    # ---- who is who -----------------------------------------------------------------------

    @mcp.tool(name="list_data_links", tags={"admin"})
    async def list_links(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, linked: bool | None = None, origin_context_id: str | None = None
    ) -> dict[str, Any]:
        """List the accounts the data sources know, each linked to a person or not, and the
        open suggestions (an account whose verified email matches a person). Pass this active
        turn's origin_context_id. In a shared conversation (a channel or group), emails are
        [[private:...]] placeholders: answer here without them, and send any the admin needs
        with send_private_details. Admin-only."""
        auth = await _auth(ctx)
        actor = await manager(runtime, auth)
        hide = await privately(runtime, auth, origin_context_id)
        names = await names_of(runtime, actor.tenant_id)
        async with store(runtime) as conn:
            found = await manage.identities(conn, schemas, actor.tenant_id, linked=linked)
            open_ = (
                [] if linked is True
                else await identity.list_candidates(conn, schemas, actor.tenant_id)
            )  # fmt: skip
        return {
            "accounts": [
                {
                    "provider": i.provider, "workspace": i.workspace, "subject": i.subject,
                    "name": i.name, "email": hide(i.email),
                    "person": names.get(i.account_id) if i.account_id else None,
                    "person_id": str(i.account_id) if i.account_id else None,
                    "proof": i.proof, "names_hidden": i.redacted,
                }
                for i in found
            ],
            "suggestions": [
                {
                    "provider": c.key.provider, "workspace": c.key.workspace,
                    "subject": c.key.subject, "email": hide(c.email),
                    "person": names.get(c.account_id), "person_id": str(c.account_id),
                }
                for c in open_
            ],
        }  # fmt: skip

    @mcp.tool(name="change_data_links", tags={"admin"})
    async def change_links(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        action: LinkAction,
        origin_context_id: str,
        provider: str = "",
        workspace: str = "",
        subject: str = "",
        person: str = "",
    ) -> dict[str, Any]:
        """Link an account the data sources know to a person (link: provider, workspace,
        subject as listed, person), turn down a suggestion (reject: the same), unlink an
        account (unlink: provider, workspace, subject), or hide or show a person's name in
        what agents read (hide_names, show_names: person). Runs on the asker's Approve card.
        Admin-only."""
        auth = await _auth(ctx)
        actor = await manager(runtime, auth)
        await require_card(
            runtime, auth, tool_name="change_data_links", origin_context_id=origin_context_id
        )
        t, by = actor.tenant_id, actor.account_id
        names = await names_of(runtime, t)
        async with store(runtime) as conn:
            try:
                match action:
                    case "link":
                        await identity.admin_link(
                            conn, schemas, tenant_id=t, by=by, admin=True,
                            key=_key(provider, workspace, subject),
                            account_id=account_of(names, person),
                        )  # fmt: skip
                    case "reject":
                        await identity.reject_candidate(
                            conn, schemas, tenant_id=t, by=by, admin=True,
                            key=_key(provider, workspace, subject),
                            account_id=account_of(names, person),
                        )  # fmt: skip
                    case "unlink":
                        await identity.unlink(
                            conn, schemas, tenant_id=t, by=by, admin=True,
                            key=_key(provider, workspace, subject),
                        )  # fmt: skip
                    case "hide_names" | "show_names":
                        await redaction.set_redacted(
                            conn, schemas, t, account_of(names, person),
                            action == "hide_names", by=by, admin=True,
                        )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None
        return _DONE

    # ---- redaction rule sets --------------------------------------------------------------

    @mcp.tool(name="list_data_rule_sets", tags={"admin"})
    async def list_rule_sets(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str = "", origin_context_id: str | None = None
    ) -> dict[str, Any]:
        """List the redaction rule sets, or show one (name) with its approved and waiting
        rules. Pass this active turn's origin_context_id. The rules name what is hidden, so
        in a shared conversation (a channel or group) they are [[private:...]] placeholders:
        answer here without them, and send them with send_private_details. Admin-only."""
        auth = await _auth(ctx)
        actor = await manager(runtime, auth)
        hide = await privately(runtime, auth, origin_context_id)
        async with store(runtime) as conn:
            try:
                if not name:
                    found = await redaction.list_sets(
                        conn, schemas, actor.tenant_id, by=actor.account_id, admin=True
                    )
                    return {"sets": [_set(s) for s in found]}
                one = await redaction.show_set(
                    conn, schemas, actor.tenant_id, name, by=actor.account_id, admin=True
                )
            except IdentityError as e:
                raise ToolError(str(e)) from None
        return {
            "set": _set(one.info),
            "rules": hide(one.rules.model_dump(mode="json")) if one.rules else None,
            "waiting_rules": (
                hide(one.pending_rules.model_dump(mode="json")) if one.pending_rules else None
            ),
        }

    @mcp.tool(name="approve_data_rule_set", tags={"admin"})
    async def approve_rule_set(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, sha: str, origin_context_id: str
    ) -> dict[str, Any]:
        """Approve a redaction rule set's waiting version (sha, as listed). Runs on the
        asker's Approve card. Admin-only."""
        auth = await _auth(ctx)
        actor = await manager(runtime, auth)
        await require_card(
            runtime, auth, tool_name="approve_data_rule_set", origin_context_id=origin_context_id
        )
        async with store(runtime) as conn:
            try:
                done = await redaction.approve(
                    conn, schemas, actor.tenant_id, name, sha, by=actor.account_id, admin=True
                )
            except IdentityError as e:
                raise refused(e) from None
        return {"set": _set(done)}


def _by_name(entry: tuple[uuid.UUID, str]) -> str:
    return entry[1]


def _set(info: redaction.SetInfo) -> dict[str, Any]:
    out = plain(info)
    out["error"] = cut(out["error"])
    return out


def _holder_name(holder: str, names: dict[uuid.UUID, str]) -> str | None:
    if not holder.startswith("account:"):
        return None
    try:
        return names.get(uuid.UUID(holder.removeprefix("account:")))
    except ValueError:
        return None


__all__ = ["register_data_admin_tools"]

"""Agent-scoped grants from a verified personal GitHub link."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, cast

import httpx
import structlog
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.oauth_github import _installations  # pyright: ignore[reportPrivateUsage]
from daimon.core.github_app_auth import (
    AppInstallationDetails,
    build_app_jwt,
    get_app_installation_details,
    installation_supports_write,
    list_installation_repositories,
    mint_installation_listing_token,
)
from daimon.core.github_requester_access import (
    linked_access_token_with_generation,
    settle_detached_refreshes,
)
from daimon.core.stores.accounts import get_account
from daimon.core.stores.github_access import (
    SecondInstallationError,
    list_agent_grants,
    other_installation_message,
    require_owner_grant_installations,
    stage_grant,
    upsert_linked_agent_authorization,
)
from daimon.core.stores.github_app_installations import get as get_installation
from daimon.core.stores.github_app_installations import get_for_update, upsert_github_app
from daimon.core.stores.github_installation_coverage import installation_covers_repo
from daimon.core.stores.github_installation_reconciliation import enqueue as enqueue_reconciliation
from daimon.core.stores.github_links import (
    get_account_link,
    get_account_link_for_update,
    get_user,
    get_user_for_update,
)
from daimon.core.stores.github_owner_grants import (
    clear_exclusion,
    installation_associated,
    upsert_owner_grant,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LinkedStatus = Literal[
    "granted",
    "granted_read_only",
    "needs_link",
    "relink",
    "no_github_access",
    "needs_install",
    "app_unusable",
    "install_pending",
    "other_installation",
    "changed_try_again",
]


# Each chat result is a status line, then a supporting line. The private link and
# setup buttons travel separately, so no text here holds a URL or a token.
NEEDS_LINK_MESSAGE = "Link GitHub to check your repo access."
RELINK_MESSAGE = "Your GitHub link needs renewing."
CHANGED_MESSAGE = "Access changed while checking.\nAsk again."
INSTALL_PENDING_MESSAGE = "Daimon is refreshing its repo list.\nTry again shortly."
GITHUB_DOWN_MESSAGE = "GitHub didn't answer.\nAsk again in chat."

_log = structlog.get_logger(__name__)


def ask_admin_message(agent_name: str, repo_label: str) -> str:
    return (
        f"Only a server admin can give {agent_name} repos.\n"
        f"Ask a server admin to say here: connect {repo_label} to {agent_name}."
    )


def no_access_message(repo_name: str) -> str:
    return (
        f"Your GitHub account can't open {repo_name}.\n"
        "Ask a server admin who can open this repo on GitHub."
    )


def owner_no_access_message(owner: str) -> str:
    return (
        f"You need access to every {owner} repo the Daimon app covers.\n"
        "Ask a server admin who can open them on GitHub."
    )


def needs_install_message(repo_name: str) -> str:
    # A repo missing from the person's installation listing may be private and
    # invisible to them, outside the app's repos, or waiting on an install, and
    # the listing cannot tell these apart, so the text claims none of them.
    return (
        f"Daimon can't reach {repo_name} with your GitHub account.\n"
        "If you can open it on GitHub, ask an owner or repo admin to add it to the Daimon app."
    )


def owner_needs_install_message(owner: str) -> str:
    return (
        f"Daimon can't reach {owner} repos with your GitHub account.\n"
        "If you can open them on GitHub, ask an owner to add them to the Daimon app."
    )


def app_unusable_message(owner: str) -> str:
    # The Daimon app is installed for this owner but cannot be used as it stands
    # (wrong account, suspended, too few permissions). Adding repos would not
    # help and the install flow is the wrong button, so no setup card goes with it.
    return (
        f"Daimon can't use its app on {owner}.\n"
        f"Ask an owner of {owner} to check the Daimon app in their GitHub settings."
    )


class _DetailUnavailable(Exception):
    """GitHub did not give a usable answer about an installation; nothing says it is unusable."""


async def _installation_detail(
    client: httpx.AsyncClient, *, private_key: str, app_id: str, installation_id: int
) -> AppInstallationDetails | None:
    """The installation's details, or None when GitHub says the App cannot use it.

    403 and 404 on the App's own request mean the installation is gone or refused
    the App: unusable. Anything else is not a verdict on the installation. A 401
    is Daimon's own App credential being rejected, not the person's link, so it
    never maps to relink. Rate limits and 5xx are transient. Those raise
    `_DetailUnavailable`, which the callers turn into the GitHub-didn't-answer
    outcome.
    """
    try:
        return await get_app_installation_details(
            client,
            jwt=build_app_jwt(private_key, app_id, now=int(time.time())),
            installation_id=installation_id,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        rate_limited = (
            exc.response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in exc.response.headers
        )
        if status in (403, 404) and not rate_limited:
            return None
        _log.error(
            "github.linked_grant.installation_detail_failed",
            installation_id=installation_id,
            status_code=status,
            app_credential_rejected=status == 401,
        )
        raise _DetailUnavailable from exc


def _github_down() -> LinkedOutcome:
    return LinkedOutcome(status="changed_try_again", message=GITHUB_DOWN_MESSAGE)


def _ability(access: Literal["read", "write"]) -> str:
    return "read and change" if access == "write" else "read"


def granted_message(agent_name: str, repo_name: str, access: Literal["read", "write"]) -> str:
    return (
        f"{agent_name} can now {_ability(access)} {repo_name}.\n"
        f"Everyone who talks to {agent_name} can use this repo through {agent_name}."
    )


def owner_granted_message(
    agent_name: str,
    owner: str,
    access: Literal["read", "write"],
    removed: list[str],
) -> str:
    status = f"{agent_name} can now {_ability(access)} {owner} repos the Daimon app covers"
    status += f", except {len(removed)} you removed." if removed else "."
    lines = [
        status,
        "New repos the app covers later join automatically. "
        f"Everyone who talks to {agent_name} can use them through {agent_name}.",
    ]
    if removed:
        lines.append(f"Still removed: {', '.join(removed)}")
    return "\n".join(lines)


class LinkedOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: LinkedStatus
    message: str


class _RepoOwner(BaseModel):
    id: int


class _RepoPayload(BaseModel):
    id: int
    full_name: str
    owner: _RepoOwner
    permissions: dict[str, bool] = Field(default_factory=dict)


def proposed_linked_grant_policy(
    permissions: dict[str, bool],
) -> tuple[Literal["read", "write"], str] | None:
    """Push, maintain or admin grants write; triage or pull grants read."""
    for key in ("admin", "maintain", "push", "triage", "pull"):
        if permissions.get(key) is True:
            return ("write" if key in ("admin", "maintain", "push") else "read", key)
    return None


async def _before_commit() -> None:
    """Test seam for revocation between GitHub's answer and the DB transaction."""


async def linked_grant(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
    repo_name: str,
    fernet: MultiFernet | None,
    client_id: str,
    client_secret: str,
    app_id: str,
    private_key: str,
    manages_now: Callable[[AsyncSession], Awaitable[bool]],
    requested_access: Literal["read", "write"] = "read",
) -> LinkedOutcome:
    if repo_name.endswith("/*"):
        return await linked_owner_grant(
            sessionmaker,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            agent_name=agent_name,
            owner=repo_name[:-2],
            fernet=fernet,
            client_id=client_id,
            client_secret=client_secret,
            app_id=app_id,
            private_key=private_key,
            manages_now=manages_now,
            access=requested_access,
        )
    if fernet is None:
        return LinkedOutcome(status="changed_try_again", message="GitHub setup is unavailable.")
    async with sessionmaker() as session:
        account = await get_account(session, account_id)
        if account is None or account.tenant_id != tenant_id or account.is_external:
            return LinkedOutcome(status="needs_link", message=NEEDS_LINK_MESSAGE)
        link = await get_account_link(session, account_id=account_id)
        if link is None:
            return LinkedOutcome(status="needs_link", message=NEEDS_LINK_MESSAGE)
        user = await get_user(session, github_user_id=link.github_user_id)
        if user is None or user.status != "active":
            return LinkedOutcome(status="relink", message=RELINK_MESSAGE)
    async with (
        httpx.AsyncClient(timeout=15.0) as client,
        settle_detached_refreshes(),
    ):
        try:
            access_token, checked_generation = await linked_access_token_with_generation(
                sessionmaker,
                client,
                user_id=link.github_user_id,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
            )
            if access_token is None:
                return LinkedOutcome(status="relink", message=RELINK_MESSAGE)
            installations = await _installations(client, access_token)
            owner = repo_name.split("/", 1)[0]
            candidates = [i for i in installations if i.owner_login.casefold() == owner.casefold()]
            covering = [
                (i, r)
                for i in candidates
                for r in i.repos
                if r.full_name.casefold() == repo_name.casefold()
            ]
            if not covering:
                return LinkedOutcome(
                    status="needs_install", message=needs_install_message(repo_name)
                )
            installation, listed_repo = covering[0]
            if not listed_repo.usable:
                return LinkedOutcome(
                    status="no_github_access",
                    message=no_access_message(repo_name),
                )
            try:
                detail = await _installation_detail(
                    client,
                    private_key=private_key,
                    app_id=app_id,
                    installation_id=installation.id,
                )
            except _DetailUnavailable:
                return _github_down()
            if (
                detail is None
                or detail.account_id != installation.owner_id
                or detail.account_login.casefold() != installation.owner_login.casefold()
                or detail.suspended_at is not None
                or not installation_supports_write(detail.permissions)
                or installation.repository_selection not in ("all", "selected")
            ):
                return LinkedOutcome(status="app_unusable", message=app_unusable_message(owner))
            async with sessionmaker() as session:
                fresh_user = await get_user(session, github_user_id=link.github_user_id)
            if fresh_user is None or fresh_user.status != "active":
                return LinkedOutcome(status="relink", message=RELINK_MESSAGE)
            response = await client.get(
                f"https://api.github.com/repos/{repo_name}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if response.status_code == 401:
                return LinkedOutcome(status="relink", message=RELINK_MESSAGE)
            if response.status_code == 404:
                return LinkedOutcome(
                    status="no_github_access",
                    message=no_access_message(repo_name),
                )
            response.raise_for_status()
            repo_payload: object = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return LinkedOutcome(status="relink", message=RELINK_MESSAGE)
            if exc.response.status_code == 403 and (
                exc.response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in exc.response.headers
            ):
                raise
            if exc.response.status_code in (403, 404):
                return LinkedOutcome(
                    status="no_github_access",
                    message=no_access_message(repo_name),
                )
            raise
    try:
        repo = _RepoPayload.model_validate(repo_payload)
    except ValidationError:
        return LinkedOutcome(status="no_github_access", message=no_access_message(repo_name))
    if repo.full_name.casefold() != repo_name.casefold():
        return LinkedOutcome(status="no_github_access", message=no_access_message(repo_name))
    repo_id = repo.id
    owner_id = repo.owner.id
    if repo_id != listed_repo.id or owner_id != installation.owner_id:
        return LinkedOutcome(status="no_github_access", message=no_access_message(repo_name))
    policy = proposed_linked_grant_policy(repo.permissions)
    if policy is None:
        return LinkedOutcome(status="no_github_access", message=no_access_message(repo_name))
    access, permission = policy
    if listed_repo.permission in ("triage", "read"):
        access = "read"
    async with sessionmaker() as session:
        original_installation = await get_installation(session, installation_id=installation.id)
        originally_present = original_installation is not None
        original_names: set[str] = (
            set(original_installation.repo_full_names) if original_installation else set()
        )
        if originally_present and not installation_covers_repo(
            original_installation, repo.full_name
        ):
            return LinkedOutcome(status="changed_try_again", message=CHANGED_MESSAGE)
    await _before_commit()

    try:
        async with sessionmaker.begin() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            current_link = await get_account_link_for_update(session, account_id=account_id)
            current_user = await get_user_for_update(session, github_user_id=link.github_user_id)
            current_installation = await get_for_update(session, installation_id=installation.id)
            if (
                current_link is None
                or current_link.github_user_id != link.github_user_id
                or current_user is None
                or current_user.status != "active"
                or current_user.link_generation != checked_generation
                or (
                    current_installation is not None
                    and (
                        not originally_present
                        or not installation_covers_repo(current_installation, repo.full_name)
                        or set(current_installation.repo_full_names) != original_names
                    )
                )
                or (originally_present and current_installation is None)
                or not await manages_now(session)
            ):
                return LinkedOutcome(status="changed_try_again", message=CHANGED_MESSAGE)
            await upsert_linked_agent_authorization(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=repo_id,
                owner_id=owner_id,
                installation_id=installation.id,
                repo_full_name=repo.full_name,
                access=access,
                github_user_id=link.github_user_id,
                account_id=account_id,
                permission=permission,
            )
            await upsert_github_app(
                session,
                installation_id=installation.id,
                account_id=installation.owner_id,
                account_login=installation.owner_login,
                account_type=installation.owner_type,
                repository_selection=installation.repository_selection,
                suspended_at=None,
            )
            await enqueue_reconciliation(
                session,
                installation_id=installation.id,
                delivery_id=f"connect:{uuid.uuid4()}",
                event="connect",
                deleted=False,
                now=datetime.now(UTC),
            )
            existing = next(
                (
                    g
                    for g in await list_agent_grants(
                        session, tenant_id=tenant_id, agent_id=agent_id
                    )
                    if g.repo_id == repo_id
                ),
                None,
            )
            await stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=repo_id,
                baseline_access=access,
                ceiling_access=access,
                granted_by_account_id=account_id,
                mount_path=existing.mount_path if existing else None,
                is_working_repo=existing.is_working_repo if existing else False,
                agent_name=agent_name,
            )
            await clear_exclusion(
                session, tenant_id=tenant_id, agent_id=agent_id, repo_full_name=repo.full_name
            )
    except SecondInstallationError as exc:
        # The transaction rolled back, so no authorization or grant row was saved.
        return LinkedOutcome(
            status="other_installation",
            message=other_installation_message(agent_name, exc.org, exc.kind),
        )
    return LinkedOutcome(
        status="granted" if access == "write" else "granted_read_only",
        message=granted_message(agent_name, repo_name, access),
    )


async def linked_owner_grant(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
    owner: str,
    fernet: MultiFernet | None,
    client_id: str,
    client_secret: str,
    app_id: str,
    private_key: str,
    manages_now: Callable[[AsyncSession], Awaitable[bool]],
    access: Literal["read", "write"],
) -> LinkedOutcome:
    """Grant all presently and later covered repositories for one owner."""
    if fernet is None:
        return LinkedOutcome(status="changed_try_again", message="GitHub setup is unavailable.")
    async with sessionmaker() as session:
        account = await get_account(session, account_id)
        if account is None or account.tenant_id != tenant_id or account.is_external:
            return LinkedOutcome(status="needs_link", message=NEEDS_LINK_MESSAGE)
        link = await get_account_link(session, account_id=account_id)
        if link is None:
            return LinkedOutcome(status="needs_link", message=NEEDS_LINK_MESSAGE)
        user = await get_user(session, github_user_id=link.github_user_id)
        if user is None or user.status != "active":
            return LinkedOutcome(status="relink", message=RELINK_MESSAGE)
    try:
        async with (
            httpx.AsyncClient(timeout=15.0) as client,
            settle_detached_refreshes(),
        ):
            token, checked_generation = await linked_access_token_with_generation(
                sessionmaker,
                client,
                user_id=link.github_user_id,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
            )
            if token is None:
                return LinkedOutcome(status="relink", message=RELINK_MESSAGE)
            installations = await _installations(client, token)
            candidates = [i for i in installations if i.owner_login.casefold() == owner.casefold()]
            if not candidates:
                return LinkedOutcome(
                    status="needs_install", message=owner_needs_install_message(owner)
                )
            checked: list[tuple[int, str, frozenset[str], list[tuple[int, str, str]]]] = []
            async with sessionmaker() as session:
                for candidate in candidates:
                    current = await get_installation(session, installation_id=candidate.id)
                    if current is not None and (
                        current.suspended_at is not None
                        or current.github_removed_at is not None
                        or current.account_login.casefold() != owner.casefold()
                    ):
                        continue
                    try:
                        detail = await _installation_detail(
                            client,
                            private_key=private_key,
                            app_id=app_id,
                            installation_id=candidate.id,
                        )
                    except _DetailUnavailable:
                        return _github_down()
                    if (
                        detail is None
                        or detail.account_id != candidate.owner_id
                        or detail.account_login.casefold() != owner.casefold()
                        or detail.suspended_at is not None
                        or not installation_supports_write(detail.permissions)
                        or candidate.repository_selection not in ("all", "selected")
                    ):
                        continue
                    first_use = current is None or not await installation_associated(
                        session, tenant_id=tenant_id, installation_id=candidate.id
                    )
                    if current is None:
                        listing_token = await mint_installation_listing_token(
                            client,
                            jwt=build_app_jwt(private_key, app_id, now=int(time.time())),
                            installation_id=candidate.id,
                        )
                        names = frozenset(
                            await list_installation_repositories(client, token=listing_token)
                        )
                    else:
                        names = frozenset(
                            current.github_repo_full_names
                            if current.github_repo_full_names is not None
                            else current.repo_full_names
                        )
                    covered = {
                        name
                        for name in names
                        if name.split("/", 1)[0].casefold() == owner.casefold()
                    }
                    listed = {repo.full_name.casefold(): repo for repo in candidate.repos}
                    if (
                        candidate.owner_type.casefold() == "user"
                        and user.login.casefold() != owner.casefold()
                    ):
                        return LinkedOutcome(
                            status="no_github_access",
                            message=owner_no_access_message(owner),
                        )
                    if candidate.owner_type.casefold() != "user" and any(
                        name.casefold() not in listed
                        or listed[name.casefold()].permission is None
                        or (
                            access == "write"
                            and listed[name.casefold()].permission
                            not in ("write", "maintain", "admin")
                        )
                        for name in covered
                    ):
                        return LinkedOutcome(
                            status="no_github_access",
                            message=owner_no_access_message(owner),
                        )
                    new_rows = [
                        (listed[name.casefold()].id, name, listed[name.casefold()].permission or "")
                        for name in covered
                        if first_use and name.casefold() in listed
                    ]
                    checked.append((candidate.id, candidate.owner_login, names, new_rows))
            if not checked:
                return LinkedOutcome(status="app_unusable", message=app_unusable_message(owner))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return LinkedOutcome(status="relink", message=RELINK_MESSAGE)
        if (
            exc.response.status_code in (403, 404)
            and exc.response.headers.get("x-ratelimit-remaining") != "0"
        ):
            return LinkedOutcome(
                status="no_github_access",
                message=owner_no_access_message(owner),
            )
        raise
    await _before_commit()
    removed: list[str] = []
    try:
        async with sessionmaker.begin() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            current_link = await get_account_link_for_update(session, account_id=account_id)
            current_user = await get_user_for_update(session, github_user_id=link.github_user_id)
            if (
                current_link is None
                or current_link.github_user_id != link.github_user_id
                or current_user is None
                or current_user.status != "active"
                or current_user.link_generation != checked_generation
                or not await manages_now(session)
            ):
                return LinkedOutcome(status="changed_try_again", message=CHANGED_MESSAGE)
            # Lock the installations, in id order, before the agent's grant fence
            # that `require_owner_grant_installations` bumps: the explicit path
            # takes them in that order too, so two grants never wait on each other.
            for installation_id, _owner_login, names, new_rows in sorted(
                checked, key=lambda entry: entry[0]
            ):
                current = await get_for_update(session, installation_id=installation_id)
                if current is not None and (
                    current.suspended_at is not None
                    or current.github_removed_at is not None
                    or current.account_login.casefold() != owner.casefold()
                    or frozenset(
                        current.github_repo_full_names
                        if current.github_repo_full_names is not None
                        else current.repo_full_names
                    )
                    != names
                ):
                    return LinkedOutcome(status="changed_try_again", message=CHANGED_MESSAGE)
                if not new_rows and not await installation_associated(
                    session, tenant_id=tenant_id, installation_id=installation_id
                ):
                    return LinkedOutcome(status="changed_try_again", message=CHANGED_MESSAGE)
            await require_owner_grant_installations(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                installation_ids={installation_id for installation_id, *_ in checked},
                agent_name=agent_name,
            )
            for installation_id, owner_login, _names, new_rows in checked:
                if new_rows:
                    for repo_id, repo_full_name, permission in new_rows:
                        await upsert_linked_agent_authorization(
                            session,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            repo_id=repo_id,
                            owner_id=next(
                                i.owner_id for i in candidates if i.id == installation_id
                            ),
                            installation_id=installation_id,
                            repo_full_name=repo_full_name,
                            access=access,
                            github_user_id=link.github_user_id,
                            account_id=account_id,
                            permission=permission,
                        )
                    candidate = next(i for i in candidates if i.id == installation_id)
                    await upsert_github_app(
                        session,
                        installation_id=installation_id,
                        account_id=candidate.owner_id,
                        account_login=candidate.owner_login,
                        account_type=candidate.owner_type,
                        repository_selection=cast(
                            Literal["all", "selected"], candidate.repository_selection
                        ),
                        suspended_at=None,
                    )
                    await enqueue_reconciliation(
                        session,
                        installation_id=installation_id,
                        delivery_id=f"connect:{uuid.uuid4()}",
                        event="connect",
                        deleted=False,
                        now=datetime.now(UTC),
                    )
                if not await installation_associated(
                    session, tenant_id=tenant_id, installation_id=installation_id
                ):
                    raise ValueError("Owner installation association was not saved")
                await upsert_owner_grant(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    installation_id=installation_id,
                    owner_login=owner_login,
                    access=access,
                    account_id=account_id,
                )
            prefix = f"{owner.casefold()}/"
            removed = sorted(
                name[len(prefix) :]
                for name in await session.scalars(
                    text(
                        "SELECT lower(repo_full_name) FROM agent_github_owner_exclusions "
                        "WHERE tenant_id = :tenant AND agent_id = :agent"
                    ).bindparams(tenant=tenant_id, agent=agent_id)
                )
                if name.startswith(prefix)
            )
    except SecondInstallationError as exc:
        # The transaction rolled back, so no authorization or grant row was saved.
        return LinkedOutcome(
            status="other_installation",
            message=other_installation_message(agent_name, exc.org, exc.kind),
        )
    return LinkedOutcome(
        status="granted" if access == "write" else "granted_read_only",
        message=owner_granted_message(agent_name, owner, access, removed),
    )

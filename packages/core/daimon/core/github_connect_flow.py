"""Browser-bound GitHub App repository connection flow.

Each step takes plain values (the path token, query parameters, cookies and a
parsed `ConnectSelection`) and returns a `ConnectOutcome`: a redirect or a view
from `github_connect_views`, with the cookies to set. `root` is the public
origin the person is on; every URL the flow builds sits under it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Literal, cast
from urllib.parse import urlencode

import httpx
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import MultiFernet
from daimon.core import github_connect_follow
from daimon.core.agent_ownership import owns_agent
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.features.service import set_agent_feature
from daimon.core.github_app_auth import (
    AppInstallationDetails,
    build_app_jwt,
    get_app_installation_details,
    installation_supports_write,
)
from daimon.core.github_connect_admin import TeamsOwnerLookup, attest_native_admin
from daimon.core.github_connect_follow import OrgProof, confirmed_offer, follow_offers
from daimon.core.github_connect_views import (
    FLOW_COOKIE,
    FLOW_COOKIE_MAX_AGE,
    SAVED_COOKIE_MAX_AGE,
    STATE_COOKIE,
    AlreadyConnectedView,
    BackLink,
    CancelledView,
    ChatReturn,
    ConnectCookie,
    ConnectErrorView,
    ConnectInstallation,
    ConnectOutcome,
    ConnectPage,
    ConnectRedirect,
    ConnectRepo,
    ConnectSelection,
    ConnectTransientErrorView,
    ConnectView,
    DoneView,
    ExpiredView,
    FollowOffer,
    InstallView,
    NoReposView,
    PendingInstallation,
    PendingView,
    SavedAgentView,
    agent_editor_view,
    confirm_url,
    install_url,
    receipt_signature,
    redact_hidden_grants,
    repo_confirmation,
    snapshot_signature,
    unverified,
    visibility_digest,
    workspace_picker_view,
)
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.github_requester_access import list_github_pages
from daimon.core.native_admins import AdminAttestationUnavailable
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_access, github_app_installations, github_connect
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import AccountIdentityRow, Role
from daimon.core.stores.github_access import list_agent_repos
from daimon.core.stores.github_follow_org import FEATURE_KEY as FOLLOW_ORG_FEATURE
from daimon.core.stores.github_follow_org import agent_follows
from daimon.core.stores.github_installation_reconciliation import enqueue as enqueue_reconciliation
from daimon.core.stores.github_request_actions import (
    finish_confirmed_requests,
    lock_confirmed_request_repos,
)
from daimon.core.stores.security_audit import append_event
from daimon.core.web_links import WebLinks
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import text as sql_text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ClientFactory = Callable[[], httpx.AsyncClient]
_log = logging.getLogger(__name__)
ADMIN_RETRY = "Couldn't confirm you're still an admin. Try again in a minute."
_CHANGED = "The agent's repos changed. Reload and try again."
_NO_LONGER_MANAGES = "You can no longer manage this agent."
_UNVERIFIED_ACCESS = "GitHub access could not be verified."
_UNREACHABLE = "Couldn't reach GitHub"
# One wall-clock budget for listing every installation and its repos. Each GitHub
# call already times out at 20 s, but they run one after another, so many
# installations or pages could hold the request open indefinitely. 60 s leaves
# room for large organizations and stays well inside Cloud Run's request timeout
# (the mcp service sets none, so the platform default of 300 s applies; see the
# `timeoutSeconds` fallback in .github/workflows/deploy.yml).
INSTALLATIONS_DEADLINE_SECONDS = 60.0


class _OwnerPayload(BaseModel):
    id: int
    login: str = ""
    type: str = ""
    avatar_url: str | None = None


class _InstallationPayload(BaseModel):
    id: int
    account: _OwnerPayload
    repository_selection: str = "all"
    suspended_at: str | None = None


class _RepoPayload(BaseModel):
    id: int
    owner: _OwnerPayload
    full_name: str = ""
    permissions: dict[str, bool] = Field(default_factory=dict[str, bool])
    private: bool = False
    archived: bool = False


class _TokenPayload(BaseModel):
    access_token: str


class _UserPayload(BaseModel):
    id: int


class _AccountLoginPayload(BaseModel):
    login: str = ""


class _InstallationRequestPayload(BaseModel):
    requester: _UserPayload
    account: _AccountLoginPayload | None = None


def _github_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


async def list_installations(
    client: httpx.AsyncClient,
    token: str,
    *,
    before_repo_fetch: Callable[[int], Awaitable[None]] | None = None,
) -> list[ConnectInstallation]:
    """Every App installation the user token sees, with its usable repos."""
    raw = await list_github_pages(client, "/user/installations", token, "installations")
    result: list[ConnectInstallation] = []
    for item in raw:
        parsed = _InstallationPayload.model_validate(item)
        installation_id = parsed.id
        owner_id = parsed.account.id
        repos: list[ConnectRepo] = []
        total = 0
        truncated = False
        last_page_full = False
        fetch_state: Literal["ok", "error"] = "ok"
        if parsed.suspended_at is None:
            if before_repo_fetch is not None:
                await before_repo_fetch(installation_id)
            try:
                for page in range(1, 21):
                    response = await client.get(
                        f"https://api.github.com/user/installations/{installation_id}/repositories",
                        params={"per_page": 100, "page": page},
                        headers=_github_headers(token),
                    )
                    response.raise_for_status()
                    payload = TypeAdapter(dict[str, object]).validate_python(response.json())
                    batch = TypeAdapter(list[dict[str, object]]).validate_python(
                        payload["repositories"]
                    )
                    if page == 1:
                        count = payload.get("total_count", 0)
                        total = count if type(count) is int else 0
                    for raw_repo in batch:
                        repo = _RepoPayload.model_validate(raw_repo)
                        if repo.owner.id != owner_id:
                            continue
                        repos.append(
                            ConnectRepo(
                                id=repo.id,
                                owner_id=owner_id,
                                installation_id=installation_id,
                                full_name=repo.full_name,
                                permission=github_connect.github_permission(repo.permissions),
                                private=repo.private,
                                archived=repo.archived,
                            )
                        )
                    last_page_full = len(batch) == 100
                    if not last_page_full:
                        break
                truncated = total > 2000 or (last_page_full and total == 0)
                total = max(total, len(repos))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                # An owner's partial list is unsafe for selection; keep the owner visible.
                repos.clear()
                fetch_state = "error"
        result.append(
            ConnectInstallation(
                id=installation_id,
                owner_id=owner_id,
                owner_login=parsed.account.login,
                repository_selection=parsed.repository_selection,
                repos=tuple(repos),
                owner_type=parsed.account.type,
                avatar_url=parsed.account.avatar_url,
                state="suspended" if parsed.suspended_at is not None else "active",
                total=total,
                truncated=truncated,
                fetch_state=fetch_state,
            )
        )
    return result


async def has_pending_installation_request(
    client: httpx.AsyncClient, *, app_id: str, private_key: str, github_user_id: int
) -> PendingInstallation:
    app_token = build_app_jwt(private_key, app_id, now=int(time.time()))
    requests = await list_github_pages(
        client, "/app/installation-requests", app_token, "installation_requests"
    )
    matching = [
        parsed
        for request in requests
        if (parsed := _InstallationRequestPayload.model_validate(request)).requester.id
        == github_user_id
    ]
    if len(matching) == 1 and matching[0].account and matching[0].account.login:
        return PendingInstallation(True, matching[0].account.login)
    return PendingInstallation(bool(matching))


def _page(view: ConnectView, *cookies: ConnectCookie) -> ConnectPage:
    return ConnectPage(view=view, cookies=cookies)


def _error(
    message: str = "This link has expired.",
    status: int = 400,
    retry_url: str | None = None,
    *,
    action: str | None = None,
) -> ConnectPage:
    return _page(
        ConnectErrorView(message=message, status=status, retry_url=retry_url, action=action)
    )


def _chat_return(requester: AccountIdentityRow | None) -> ChatReturn | None:
    if requester is None:
        return None
    return ChatReturn(platform=requester.platform, workspace_id=requester.external_id)


def _flow_cookie(root: str, value: str, max_age: int) -> ConnectCookie:
    return ConnectCookie(
        name=FLOW_COOKIE, value=value, max_age=max_age, secure=root.startswith("https://")
    )


class GitHubConnectFlow:
    """The connect, callback, setup and confirm steps, with their collaborators."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        client_factory: ClientFactory | None = None,
        deployment_default: DeploymentDefault | None = None,
        anthropic: AsyncAnthropic | None = None,
        teams_owner_ids: TeamsOwnerLookup | None = None,
    ) -> None:
        config = settings.github_app
        if (
            config.client_id is None
            or config.client_secret is None
            or config.app_slug is None
            or config.app_id is None
            or config.private_key is None
        ):
            raise ValueError("GitHub connection is not configured")
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._fernet = fernet
        self._factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=20.0, follow_redirects=False)
        )
        self._deployment_default = deployment_default
        self._anthropic = anthropic
        self._teams_owner_ids = teams_owner_ids
        self._client_id = config.client_id
        self._secret = config.client_secret.get_secret_value()
        self._app_slug = config.app_slug
        self._app_id = config.app_id
        self._private_key = config.private_key.get_secret_value()

    def _back(
        self, invitation: github_connect.Invitation | None, requester: AccountIdentityRow | None
    ) -> BackLink | None:
        """The agent's GitHub tab for a link minted in the web app, else the chat it came from."""
        if invitation is not None and invitation.origin_platform == "web":
            links = WebLinks.from_settings(self._settings.web)
            # Every person may open an agent's page, so the role does not matter here.
            url = (
                links.for_tenant(invitation.tenant_id, admin=False).agent(
                    invitation.agent_ma_id, "github"
                )
                if links is not None and invitation.agent_ma_id
                else None
            )
            return None if url is None else BackLink(url=url, platform="web")
        if requester is None:
            return None
        match requester.platform:
            case "discord":
                url = f"https://discord.com/channels/{requester.external_id}"
                return BackLink(url=url, platform="discord")
            case "slack" | "teams" as platform:
                return BackLink(url="", platform=platform)
            case _:
                return None

    async def _attest(
        self, requester: AccountIdentityRow, db_session: AsyncSession | None = None
    ) -> bool:
        return await attest_native_admin(
            requester=requester,
            settings=self._settings,
            sessionmaker=self._sessionmaker,
            fernet=self._fernet,
            db_session=db_session,
            teams_owner_ids=self._teams_owner_ids,
        )

    async def _revoke_user_token(self, token: str) -> bool:
        try:
            async with self._factory() as client:
                revocation = await client.request(
                    "DELETE",
                    f"https://api.github.com/applications/{self._client_id}/token",
                    auth=(self._client_id, self._secret),
                    json={"access_token": token},
                    headers={"Accept": "application/vnd.github+json"},
                )
                revocation.raise_for_status()
        except httpx.HTTPError:
            _log.warning("GitHub connection token revocation failed")
            return False
        return True

    async def _live_agent(
        self, invitation: github_connect.Invitation
    ) -> BetaManagedAgentsAgent | None:
        """The link's agent as Managed Agents has it now; None if it can't be read."""
        if self._anthropic is None or invitation.agent_id is None or not invitation.agent_ma_id:
            return None
        try:
            return await find_agent_by_derived_uuid(
                self._anthropic,
                tenant_id=invitation.tenant_id,
                agent_id=invitation.agent_id,
                records=self._sessionmaker,
            )
        except APIError:
            return None

    async def _may_manage_agent(
        self,
        session: AsyncSession,
        invitation: github_connect.Invitation,
        requester: AccountIdentityRow,
        live: BetaManagedAgentsAgent | None,
    ) -> bool:
        if invitation.agent_id is None:
            return True
        if invitation.operator_issued:
            return requester.tenant_id == invitation.tenant_id and requester.role == Role.ADMIN
        if self._deployment_default is None:
            return False
        if live is None or requester.tenant_id != invitation.tenant_id:
            return False
        # A current owner manages their own agent without platform authority.
        if await self._requester_owns_agent(session, invitation, live):
            return True
        # Only platform authority enters account roles. Preserve the existing
        # native snapshot for viewing this link; every save separately attests
        # live authority below. Delegates need a fresh check even to view it.
        if requester.role is Role.ADMIN:
            return True
        try:
            return await self._attest(requester, db_session=session)
        except AdminAttestationUnavailable:
            return False

    async def _requester_owns_agent(
        self,
        session: AsyncSession,
        invitation: github_connect.Invitation,
        live: BetaManagedAgentsAgent | None,
    ) -> bool:
        """Whether the link's requester owns the link's agent right now, as a person.

        Read from the owner tables at each call. A defaults-managed agent, an agent
        that can't be read, an operator-issued link and a person from another
        organisation never count.
        """
        if invitation.agent_id is None or invitation.operator_issued or live is None:
            return False
        if live.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
            return False
        return await owns_agent(
            session,
            tenant_id=invitation.tenant_id,
            ma_agent_id=invitation.agent_ma_id,
            account_id=invitation.requester_account_id,
        )

    async def _successful_view(
        self,
        state: str,
        cookie: str,
        invitation_hash: str = "",
        visible_ids: set[int] | None = None,
        installations: Sequence[ConnectInstallation] = (),
    ) -> ConnectView | None:
        async with self._sessionmaker() as session:
            invitation = await github_connect.successful_confirmation(
                session, state=state, cookie=cookie, invitation_hash=invitation_hash
            )
            authenticated = invitation is not None
            if invitation is None and invitation_hash:
                status, candidate = await github_connect.invitation_status(
                    session, invitation_hash, settings=self._settings
                )
                if status == "used" and candidate is not None:
                    invitation = candidate
            if invitation is None:
                return None
            requester = await get_account_with_tenant(
                session, account_id=invitation.requester_account_id
            )
            if invitation.agent_id is not None:
                if not authenticated:
                    return AlreadyConnectedView(count=invitation.connected_repo_count)
                live = await self._live_agent(invitation)
                if requester is None or not await self._may_manage_agent(
                    session, invitation, requester, live
                ):
                    return ConnectErrorView(message=_NO_LONGER_MANAGES, status=403)
            agent_grants = (
                await list_agent_repos(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
                if invitation.agent_id is not None
                else []
            )
            following = (
                invitation.agent_id is not None
                and invitation.agent_ma_id is not None
                and requester is not None
                and await self._follows(session, invitation, requester)
            )
        if invitation.agent_id is not None:
            return SavedAgentView(
                agent_name=invitation.agent_name or "Agent",
                grants=redact_hidden_grants(agent_grants, visible_ids or ()),
                visible_repo_ids=sorted(visible_ids or ()),
                back_to_chat=_chat_return(requester),
                installations=list(installations),
                back=self._back(invitation, requester),
                following=following,
            )
        return AlreadyConnectedView(
            count=invitation.connected_repo_count,
            back_to_chat=_chat_return(requester),
            agent_name=invitation.agent_name,
            back=self._back(invitation, requester),
        )

    async def _follows(
        self,
        session: AsyncSession,
        invitation: github_connect.Invitation,
        requester: AccountIdentityRow,
    ) -> bool:
        return invitation.agent_ma_id is not None and await agent_follows(
            session,
            self._settings,
            tenant_id=invitation.tenant_id,
            platform=requester.platform,
            ma_agent_id=invitation.agent_ma_id,
        )

    async def _provable_offers(
        self, token: str, offers: Sequence[FollowOffer]
    ) -> tuple[FollowOffer, ...]:
        return await github_connect_follow.org_admin_offers(
            self._factory,
            token,
            offers,
            app_jwt=build_app_jwt(self._private_key, self._app_id, now=int(time.time())),
        )

    async def start(self, root: str, token: str) -> ConnectOutcome:
        """Open a link: bind a new flow to this browser and send it to GitHub sign-in.

        The redirect sets `FLOW_COOKIE` and `STATE_COOKIE` for 15 minutes.
        """
        invitation_hash = github_connect.digest(token)
        async with self._sessionmaker() as session:
            status, invitation = await github_connect.invitation_status(
                session, invitation_hash, settings=self._settings
            )
            if status != "active":
                if invitation is None:
                    return _error()
                requester = await get_account_with_tenant(
                    session, account_id=invitation.requester_account_id
                )
                if status == "used":
                    return _page(
                        AlreadyConnectedView(
                            count=invitation.connected_repo_count,
                            back_to_chat=_chat_return(requester),
                            agent_name=invitation.agent_name,
                            back=self._back(invitation, requester),
                        )
                    )
                if status == "requester_left":
                    return _error(
                        "This link was made by someone who's no longer an admin here. "
                        "Ask an admin for a new link."
                    )
                if status == "expired":
                    return _page(ExpiredView(requester_label=invitation.requester_label))
                return _error()
        state = secrets.token_urlsafe(32)
        cookie = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        async with self._sessionmaker.begin() as session:
            if (
                await github_connect.get_invitation(
                    session, invitation_hash, settings=self._settings
                )
                is None
            ):
                return _error()
            await github_connect.create_flow(
                session,
                invitation_hash=invitation_hash,
                state=state,
                cookie=cookie,
                encrypted_verifier=encrypt_token(self._fernet, verifier),
                encrypted_invitation_token=encrypt_token(self._fernet, token),
            )
        params = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": f"{root}/oauth/github/callback",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        secure = root.startswith("https://")
        return ConnectRedirect(
            url=f"https://github.com/login/oauth/authorize?{params}",
            cookies=tuple(
                ConnectCookie(name=name, value=value, max_age=FLOW_COOKIE_MAX_AGE, secure=secure)
                for name, value in ((FLOW_COOKIE, cookie), (STATE_COOKIE, state))
            ),
        )

    async def callback(
        self, root: str, *, state: str, code: str, flow_cookie: str
    ) -> ConnectOutcome:
        """GitHub's OAuth return: exchange the code (with the PKCE verifier) for a user token.

        Only the browser holding `FLOW_COOKIE` can finish; it is sent on to confirm.
        """
        if not code:
            async with self._sessionmaker() as session:
                refused_flow = await github_connect.get_flow(
                    session, state=state, cookie=flow_cookie, settings=self._settings
                )
                refused_invitation = (
                    await github_connect.get_invitation(
                        session, refused_flow.invitation_hash, settings=self._settings
                    )
                    if refused_flow is not None
                    else None
                )
                refused_requester = (
                    await get_account_with_tenant(
                        session, account_id=refused_invitation.requester_account_id
                    )
                    if refused_invitation is not None
                    else None
                )
            return _page(
                CancelledView(
                    back_to_chat=_chat_return(refused_requester),
                    back=self._back(refused_invitation, refused_requester),
                )
            )
        async with self._sessionmaker() as session:
            flow = await github_connect.get_flow(
                session, state=state, cookie=flow_cookie, settings=self._settings
            )
        if flow is None or flow.encrypted_user_token is not None:
            return _error()
        verifier = decrypt_token(self._fernet, flow.encrypted_verifier)
        try:
            async with self._factory() as client:
                token_response = await client.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._secret,
                        "code": code,
                        "redirect_uri": f"{root}/oauth/github/callback",
                        "code_verifier": verifier,
                    },
                    headers={"Accept": "application/json"},
                )
                token_response.raise_for_status()
                user_token = _TokenPayload.model_validate(token_response.json()).access_token
                user_response = await client.get(
                    "https://api.github.com/user", headers=_github_headers(user_token)
                )
                user_response.raise_for_status()
                user_id = _UserPayload.model_validate(user_response.json()).id
        except (httpx.HTTPError, ValueError):
            retry_token = (
                decrypt_token(self._fernet, flow.encrypted_invitation_token)
                if flow.encrypted_invitation_token is not None
                else None
            )
            retry_url = f"{root}/oauth/github/connect/{retry_token}" if retry_token else None
            return _page(
                ConnectTransientErrorView(message=_UNREACHABLE, status=502, retry_url=retry_url)
            )
        async with self._sessionmaker.begin() as session:
            if (
                await github_connect.get_flow(
                    session, state=state, cookie=flow_cookie, settings=self._settings
                )
                is None
            ):
                return _error()
            saved = await github_connect.set_user_token(
                session,
                state=state,
                encrypted_token=encrypt_token(self._fernet, user_token),
                github_user_id=user_id,
            )
            if not saved:
                return _error()
        return ConnectRedirect(url=confirm_url(root, state))

    async def setup(
        self, root: str, *, state: str, flow_cookie: str, state_cookie: str
    ) -> ConnectOutcome:
        """GitHub's post-install return; `state_cookie` stands in when GitHub drops `state`."""
        # GitHub's installation_id is untrusted. Confirmation always re-lists as the user.
        state = state or state_cookie
        async with self._sessionmaker() as session:
            flow = await github_connect.get_flow(
                session, state=state, cookie=flow_cookie, settings=self._settings
            )
        if flow is None or flow.encrypted_user_token is None:
            return _error()
        return ConnectRedirect(url=confirm_url(root, state))

    async def cancel(self, root: str, *, state: str, flow_cookie: str) -> ConnectOutcome:
        """Cancel the flow and revoke its GitHub user token."""
        async with self._sessionmaker.begin() as session:
            cancelled = await github_connect.cancel_flow(session, state=state, cookie=flow_cookie)
        if cancelled is None:
            used = await self._successful_view(state, flow_cookie)
            return _page(used if used is not None else CancelledView())
        async with self._sessionmaker() as session:
            invitation = await github_connect.get_invitation(
                session, cancelled.invitation_hash, settings=self._settings
            )
            requester = (
                await get_account_with_tenant(session, account_id=invitation.requester_account_id)
                if invitation is not None
                else None
            )
        if cancelled.encrypted_user_token is not None:
            token = decrypt_token(self._fernet, cancelled.encrypted_user_token)
            if await self._revoke_user_token(token):
                async with self._sessionmaker.begin() as session:
                    await github_connect.finish_cancel_revocation(
                        session, state=state, cookie=flow_cookie
                    )
        return _page(
            CancelledView(
                back_to_chat=_chat_return(requester), back=self._back(invitation, requester)
            )
        )

    async def load(self, root: str, *, state: str, flow_cookie: str) -> ConnectOutcome:
        """The confirm page: the agent editor or the workspace picker, or why not."""
        return await self._confirm(root, state, flow_cookie, "", None)

    async def save(
        self, root: str, *, flow_cookie: str, selection: ConnectSelection
    ) -> ConnectOutcome:
        """Save a selection; a success sets `FLOW_COOKIE` for a week so its receipt reloads."""
        invitation_hash = selection.invitation
        expected = receipt_signature(selection.state, invitation_hash, self._secret)
        if not hmac.compare_digest(selection.receipt, expected):
            invitation_hash = ""
        return await self._confirm(root, selection.state, flow_cookie, invitation_hash, selection)

    async def _confirm(
        self,
        root: str,
        state: str,
        cookie: str,
        invitation_hash: str,
        selection: ConnectSelection | None,
    ) -> ConnectOutcome:
        used = await self._successful_view(state, cookie, invitation_hash)
        if used is not None:
            return _page(used)
        retry_confirm_url = confirm_url(root, state)

        def transient_error(message: str, status: int) -> ConnectPage:
            """An upstream failure the person can retry.

            After a save the retry re-posts the selection to the bare confirm
            path, so state stays out of the URL.
            """
            if selection is not None:
                return _page(
                    ConnectTransientErrorView(
                        message=message,
                        status=status,
                        retry_url=confirm_url(root),
                        retry_selection=selection,
                    )
                )
            return _page(
                ConnectTransientErrorView(
                    message=message, status=status, retry_url=retry_confirm_url
                )
            )

        async with self._sessionmaker() as session:
            flow = await github_connect.get_flow(
                session, state=state, cookie=cookie, settings=self._settings
            )
            if flow is None or flow.encrypted_user_token is None or flow.github_user_id is None:
                used = await self._successful_view(state, cookie, invitation_hash)
                return _page(used) if used is not None else _error()
            invitation = await github_connect.get_invitation(
                session, flow.invitation_hash, settings=self._settings
            )
            if invitation is None:
                used = await self._successful_view(state, cookie, invitation_hash)
                return _page(used) if used is not None else _error()
            requester = await get_account_with_tenant(
                session, account_id=invitation.requester_account_id
            )
            if requester is None:
                return _error()
            agent_repos = (
                await list_agent_repos(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
                if invitation.agent_id is not None
                else []
            )
            already_added = frozenset(
                repo.repo_id for repo in agent_repos if repo.status == "active"
            )
        token = decrypt_token(self._fernet, flow.encrypted_user_token)
        observed_refreshes: dict[int, datetime | None] = {}

        async def remember_installation_refresh(installation_id: int) -> None:
            # Capture before GitHub's list request so a newer reconciliation
            # cannot be overwritten by the positive evidence from this save.
            async with self._sessionmaker() as session:
                installation = await github_app_installations.get(
                    session, installation_id=installation_id
                )
                observed_refreshes[installation_id] = (
                    installation.github_refreshed_at if installation is not None else None
                )

        try:
            async with self._factory() as client, asyncio.timeout(INSTALLATIONS_DEADLINE_SECONDS):
                installations = await list_installations(
                    client,
                    token,
                    before_repo_fetch=remember_installation_refresh
                    if selection is not None
                    else None,
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError):
            used = await self._successful_view(state, cookie, invitation_hash)
            return _page(used) if used is not None else transient_error(_UNREACHABLE, 502)
        # Repo id: needs write. Nothing marks a repo as still needed today; the
        # editor and picker keep rendering the state for links that carry it.
        needed: dict[int, bool] = {}
        app_install_url = install_url(self._app_slug, state)
        cancel_url = confirm_url(root, state, cancel=True)
        if invitation.agent_id is not None:
            return await self._confirm_agent(
                root=root,
                agent_id=invitation.agent_id,
                github_user_id=flow.github_user_id,
                state=state,
                cookie=cookie,
                invitation_hash=invitation_hash,
                selection=selection,
                flow=flow,
                invitation=invitation,
                requester=requester,
                installations=installations,
                needed=needed,
                token=token,
                observed_refreshes=observed_refreshes,
                cancel_url=cancel_url,
                app_install_url=app_install_url,
                transient_error=transient_error,
            )
        github_user_id = flow.github_user_id
        if selection is not None:
            try:
                selected_ids = [int(value) for value in selection.repo]
            except ValueError:
                return _page(unverified(retry_confirm_url))
            # Repos already on the agent keep their access; only new ticks count.
            selected_ids = [repo_id for repo_id in selected_ids if repo_id not in already_added]
            if not selected_ids:
                return _page(
                    workspace_picker_view(
                        root=root,
                        state=state,
                        invitation_hash=flow.invitation_hash,
                        secret=self._secret,
                        cancel_url=cancel_url,
                        installations=installations,
                        platform=requester.platform,
                        workspace=invitation.workspace_label,
                        agent_name=invitation.agent_name,
                        selection_error="Select at least one repo",
                        already_added=already_added,
                        needed=needed,
                        requested_repo=invitation.requested_repo,
                        install_url=app_install_url,
                    )
                )
            visible = {
                repo.id: repo for install in installations for repo in install.repos if repo.usable
            }
            if len(set(selected_ids)) != len(selected_ids):
                return _page(unverified(retry_confirm_url))
            if any(repo_id not in visible for repo_id in selected_ids):
                return _page(unverified(retry_confirm_url, 403))
            access = selection.access[0] if selection.access else "read"
            if access not in ("read", "write"):
                return _page(unverified(retry_confirm_url))
            app_jwt = build_app_jwt(self._private_key, self._app_id, now=int(time.time()))
            by_installation = {installation.id: installation for installation in installations}
            try:
                async with self._factory() as client:
                    details = [
                        await get_app_installation_details(
                            client, jwt=app_jwt, installation_id=installation_id
                        )
                        for installation_id in sorted(
                            {visible[repo_id].installation_id for repo_id in selected_ids}
                        )
                    ]
            except (httpx.HTTPError, ValueError):
                return transient_error(_UNREACHABLE, 502)
            if any(
                detail.account_id != by_installation[detail.installation_id].owner_id
                or detail.account_login.casefold()
                != by_installation[detail.installation_id].owner_login.casefold()
                or detail.suspended_at is not None
                for detail in details
            ):
                return _error(_UNVERIFIED_ACCESS, 403, retry_confirm_url)
            installation_writes = {
                detail.installation_id: installation_supports_write(detail.permissions)
                for detail in details
            }
            if any(
                not installation_writes.get(visible[repo_id].installation_id, False)
                for repo_id in selected_ids
            ):
                return _error(
                    "The GitHub App needs write access to connect these repos.",
                    403,
                    retry_confirm_url,
                )
            repos: list[github_connect.RepoConfirmation] = [
                repo_confirmation(
                    visible[repo_id],
                    requested=access,
                    needs_write=bool(needed.get(repo_id)),
                    installation_writes=installation_writes,
                )
                for repo_id in selected_ids
            ]
            live = await self._live_agent(invitation)
            still_admin = False
            if not invitation.operator_issued:
                if (
                    invitation.requester_platform_user_id is not None
                    and invitation.requester_platform_user_id != requester.platform_user_id
                ):
                    return _error(_NO_LONGER_MANAGES, 403, retry_confirm_url)
                try:
                    still_admin = await self._attest(requester)
                except AdminAttestationUnavailable:
                    return transient_error(ADMIN_RETRY, 503)
                if not still_admin:
                    return _error(_NO_LONGER_MANAGES, 403, retry_confirm_url)
            try:
                async with self._sessionmaker.begin() as session:
                    prelocked_request_ids = await lock_confirmed_request_repos(
                        session,
                        tenant_id=invitation.tenant_id,
                        approved_by_account_id=invitation.requester_account_id,
                        extra_repo_ids={repo.repo_id for repo in repos},
                    )
                    saved = await github_connect.confirm(
                        session,
                        state=state,
                        cookie=cookie,
                        github_user_id=github_user_id,
                        repos=repos,
                        verified_platform_admin=still_admin,
                        requester_manages_agent=self._deployment_default is not None
                        and await self._may_manage_agent(session, invitation, requester, live),
                    )
                    if saved:
                        await _record_installations(session, details, repos, observed_refreshes)
                        await github_connect.activate_confirmed_agent(
                            session, invitation=invitation, repos=repos, repo_locks_held=True
                        )
                        resumed_requests = await finish_confirmed_requests(
                            session,
                            tenant_id=invitation.tenant_id,
                            approved_by_account_id=invitation.requester_account_id,
                            prelocked_request_ids=prelocked_request_ids,
                        )
                        if not resumed_requests:
                            await github_connect.queue_connect_followup(
                                session, invitation=invitation, repos=repos
                            )
                        await append_event(
                            session,
                            tenant_id=invitation.tenant_id,
                            account_id=invitation.requester_account_id,
                            agent_id=invitation.agent_id,
                            platform=requester.platform,
                            platform_user_id=requester.platform_user_id,
                            tool_name="github_connect",
                            operation="github_connect",
                            outcome="allowed",
                            reason="confirmed",
                            github_repo_ids=[repo.repo_id for repo in repos],
                        )
            except github_app_installations.StaleInstallationSnapshotError:
                return _error("GitHub access changed while saving.", 409, retry_confirm_url)
            except github_connect.PinnedAgentConnectionError:
                await self._revoke_user_token(token)
                return _error(github_connect.PINNED_AGENT_MESSAGE)
            except (
                github_connect.GrantReassignmentError,
                github_access.InstallationLimitError,
                github_access.SessionVaultBudgetError,
                github_access.ReadOnlyMultiInstallationError,
            ) as exc:
                await self._revoke_user_token(token)
                return _error(str(exc))
            except ValueError:
                await self._revoke_user_token(token)
                return _error(
                    "This connection could not be completed. Start a new GitHub connection."
                )
            if not saved:
                await self._revoke_user_token(token)
                used = await self._successful_view(state, cookie, invitation_hash)
                return _page(used) if used is not None else _error()
            async with self._sessionmaker() as session:
                requester_github_id = await github_connect.requester_linked_github_user_id(
                    session, account_id=invitation.requester_account_id
                )
            await self._revoke_user_token(token)
            async with self._sessionmaker() as session:
                await github_connect.successful_confirmation(session, state=state, cookie=cookie)
            return _page(
                DoneView(
                    count=len(repos),
                    back_to_chat=ChatReturn(
                        platform=requester.platform, workspace_id=requester.external_id
                    ),
                    requester_label=invitation.requester_label,
                    same_person=requester_github_id == github_user_id,
                    agent_name=invitation.agent_name,
                    back=self._back(invitation, requester),
                ),
                _flow_cookie(root, cookie, SAVED_COOKIE_MAX_AGE),
            )

        usable_repos = [
            repo for installation in installations for repo in installation.repos if repo.usable
        ]
        pending = PendingInstallation(False)
        if not usable_repos:
            try:
                async with self._factory() as client:
                    pending = await has_pending_installation_request(
                        client,
                        app_id=self._app_id,
                        private_key=self._private_key,
                        github_user_id=github_user_id,
                    )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return transient_error(_UNREACHABLE, 502)
        if pending.found:
            return _page(
                PendingView(
                    check_url=retry_confirm_url,
                    cancel_url=cancel_url,
                    organization=pending.account_login,
                )
            )
        if not installations:
            return _page(InstallView(install_url=app_install_url, cancel_url=cancel_url))
        if not usable_repos:
            link = (
                f"{root}/oauth/github/connect/"
                f"{decrypt_token(self._fernet, flow.encrypted_invitation_token)}"
                if flow.encrypted_invitation_token is not None
                else ""
            )
            return _page(NoReposView(share_link=link, install_url=app_install_url))
        place = {"discord": "server", "teams": "organisation"}.get(requester.platform, "workspace")
        return _page(
            workspace_picker_view(
                root=root,
                state=state,
                invitation_hash=flow.invitation_hash,
                secret=self._secret,
                cancel_url=cancel_url,
                installations=installations,
                platform=requester.platform,
                workspace=invitation.workspace_label or f"this {place}",
                agent_name=invitation.agent_name,
                already_added=already_added,
                needed=needed,
                requested_repo=invitation.requested_repo,
                install_url=app_install_url,
            )
        )

    async def _confirm_agent(
        self,
        *,
        root: str,
        agent_id: uuid.UUID,
        github_user_id: int,
        state: str,
        cookie: str,
        invitation_hash: str,
        selection: ConnectSelection | None,
        flow: github_connect.Flow,
        invitation: github_connect.Invitation,
        requester: AccountIdentityRow,
        installations: list[ConnectInstallation],
        needed: dict[int, bool],
        token: str,
        observed_refreshes: dict[int, datetime | None],
        cancel_url: str,
        app_install_url: str,
        transient_error: Callable[[str, int], ConnectPage],
    ) -> ConnectOutcome:
        """The agent editor: show one agent's repos, or save adds, removals and working repo."""
        retry_confirm_url = confirm_url(root, state)
        live = await self._live_agent(invitation)
        async with self._sessionmaker() as session:
            await session.execute(sql_text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            current_requester = await get_account_with_tenant(
                session, account_id=invitation.requester_account_id
            )
            if current_requester is None or not await self._may_manage_agent(
                session, invitation, current_requester, live
            ):
                return _error(_NO_LONGER_MANAGES, 403)
            grants = await list_agent_repos(
                session, tenant_id=invitation.tenant_id, agent_id=agent_id
            )
            snapshot = await github_connect.agent_repo_snapshot(
                session, tenant_id=invitation.tenant_id, agent_id=agent_id
            )
            following = await self._follows(session, invitation, requester)
        # Following hands the agent repos on its own, so only a workspace admin offers it.
        offers = follow_offers(
            installations,
            grants,
            may_follow=invitation.agent_ma_id is not None and current_requester.role is Role.ADMIN,
            following=following,
        )
        if selection is None:
            pending_installation = PendingInstallation(False)
            if not any(repo.usable for item in installations for repo in item.repos) and all(
                item.fetch_state == "ok" for item in installations
            ):
                try:
                    async with self._factory() as client:
                        pending_installation = await has_pending_installation_request(
                            client,
                            app_id=self._app_id,
                            private_key=self._private_key,
                            github_user_id=github_user_id,
                        )
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    return transient_error(_UNREACHABLE, 502)
            return _page(
                agent_editor_view(
                    root=root,
                    state=state,
                    invitation_hash=flow.invitation_hash,
                    secret=self._secret,
                    agent_name=invitation.agent_name or "Agent",
                    workspace=invitation.workspace_label,
                    platform=requester.platform,
                    grants=grants,
                    installations=installations,
                    snapshot=snapshot,
                    cancel_url=cancel_url,
                    install_url=app_install_url,
                    needed=needed,
                    requested_repo=invitation.requested_repo,
                    pending_installation=pending_installation,
                    github_user_id=github_user_id,
                    follow=await self._provable_offers(token, offers),
                    following=following,
                )
            )
        posted_snapshot = selection.snapshot
        if not hmac.compare_digest(
            selection.snapshot_sig,
            snapshot_signature(state, flow.invitation_hash, posted_snapshot, self._secret),
        ):
            return _page(unverified(retry_confirm_url))
        posted_visibility = selection.visibility
        if not hmac.compare_digest(
            selection.visibility_sig,
            snapshot_signature(state, flow.invitation_hash, posted_visibility, self._secret),
        ):
            return _page(unverified(retry_confirm_url))
        if posted_visibility != visibility_digest(installations):
            return _error(_CHANGED, 409, retry_confirm_url)
        try:
            selected_ids = [int(value) for value in selection.repo]
            removed_ids = [int(value) for value in selection.remove]
        except ValueError:
            return _page(unverified(retry_confirm_url))
        access_values = selection.access
        working_values = selection.working
        if (
            (
                bool(access_values)
                and (len(access_values) != 1 or access_values[0] not in ("read", "write"))
            )
            or len(working_values) != 1
            or len(selected_ids) != len(set(selected_ids))
            or len(removed_ids) != len(set(removed_ids))
        ):
            return _page(unverified(retry_confirm_url))
        selected_access: dict[int, str] = {}
        for repo_id in selected_ids:
            scoped = selection.repo_access.get(str(repo_id), ())
            if scoped:
                if len(scoped) != 1 or scoped[0] not in ("read", "write"):
                    return _page(unverified(retry_confirm_url))
                selected_access[repo_id] = scoped[0]
            elif access_values:
                selected_access[repo_id] = access_values[0]
            else:
                return _page(unverified(retry_confirm_url))
        visible = {
            repo.id: repo
            for installation in installations
            for repo in installation.repos
            if repo.usable
        }
        old = {repo.repo_id: repo for repo in grants}
        repairable = {repo.repo_id for repo in grants if repo.repo_id in needed}
        if selection.unknown_fields or set(selection.repo_access) - {
            str(repo_id) for repo_id in repairable
        }:
            return _page(unverified(retry_confirm_url))
        working_choice = working_values[0]
        if working_choice not in ("keep", "clear"):
            try:
                working_id = int(working_choice)
            except ValueError:
                return _page(unverified(retry_confirm_url))
        else:
            working_id = None
        if any(repo_id in old and repo_id not in repairable for repo_id in selected_ids):
            return _error(_UNVERIFIED_ACCESS, 403, retry_confirm_url)
        if (
            any(repo_id not in visible for repo_id in selected_ids)
            or any(repo_id not in old for repo_id in removed_ids)
            or bool(set(selected_ids) & set(removed_ids))
            or (
                working_id is not None
                and (
                    working_id not in visible
                    or (working_id not in old and working_id not in selected_ids)
                    or working_id in removed_ids
                )
            )
        ):
            return _error(_UNVERIFIED_ACCESS, 403, retry_confirm_url)
        follow_id: int | None = None
        chosen: FollowOffer | None = None
        if selection.follow_org:
            try:
                follow_id = int(selection.follow_org[0])
            except ValueError:
                follow_id = None
            if len(selection.follow_org) != 1 or follow_id is None:
                return _page(unverified(retry_confirm_url))
            held_after = (set(old) - set(removed_ids)) | set(selected_ids)
            chosen = confirmed_offer(
                offers,
                installation_id=follow_id,
                held_after=held_after,
                installations_after={
                    repo.installation_id
                    for repo in grants
                    if repo.repo_id in held_after and repo.installation_id is not None
                }
                | {visible[repo_id].installation_id for repo_id in selected_ids},
            )
            if chosen is None:
                return _error("Give the agent every repo of the org first.", 400, retry_confirm_url)
        if not selected_ids and not removed_ids and working_choice == "keep" and follow_id is None:
            return _page(
                agent_editor_view(
                    root=root,
                    state=state,
                    invitation_hash=flow.invitation_hash,
                    secret=self._secret,
                    agent_name=invitation.agent_name or "Agent",
                    workspace=invitation.workspace_label,
                    platform=requester.platform,
                    grants=grants,
                    installations=installations,
                    snapshot=snapshot,
                    cancel_url=cancel_url,
                    install_url=app_install_url,
                    needed=needed,
                    requested_repo=invitation.requested_repo,
                    selection_error="Choose a change to save.",
                    follow=await self._provable_offers(token, offers),
                    following=following,
                )
            )
        if posted_snapshot != hashlib.sha256(snapshot.encode()).hexdigest():
            return _error(_CHANGED, 409, retry_confirm_url)
        app_jwt = build_app_jwt(self._private_key, self._app_id, now=int(time.time()))
        by_installation = {install.id: install for install in installations}
        try:
            async with self._factory() as client:
                details = [
                    await get_app_installation_details(
                        client, jwt=app_jwt, installation_id=installation_id
                    )
                    for installation_id in sorted(
                        {visible[repo_id].installation_id for repo_id in selected_ids}
                    )
                ]
        except (httpx.HTTPError, ValueError):
            return transient_error(_UNREACHABLE, 502)
        if any(
            detail.account_id != by_installation[detail.installation_id].owner_id
            or detail.account_login.casefold()
            != by_installation[detail.installation_id].owner_login.casefold()
            or detail.suspended_at is not None
            for detail in details
        ):
            return _error(_UNVERIFIED_ACCESS, 403, retry_confirm_url)
        installation_writes = {
            detail.installation_id: installation_supports_write(detail.permissions)
            for detail in details
        }
        if any(
            not installation_writes.get(visible[repo_id].installation_id, False)
            for repo_id in selected_ids
        ):
            return _error(
                "The GitHub App needs write access to connect these repos.",
                403,
                retry_confirm_url,
            )
        repos = [
            repo_confirmation(
                visible[repo_id],
                requested=cast(Literal["read", "write"], selected_access[repo_id]),
                needs_write=bool(needed.get(repo_id)),
                installation_writes=installation_writes,
            )
            for repo_id in selected_ids
        ]
        still_admin = False
        acts_as_owner = False
        if not invitation.operator_issued:
            if (
                invitation.requester_platform_user_id is not None
                and invitation.requester_platform_user_id != requester.platform_user_id
            ):
                return _error(_NO_LONGER_MANAGES, 403, retry_confirm_url)
            # The requester still stands as an owner of this agent, or is still a
            # native admin on the platform. The owner is re-read in the write
            # transaction below, so a removal in between refuses the save.
            async with self._sessionmaker() as session:
                acts_as_owner = await self._requester_owns_agent(session, invitation, live)
            # Following hands the agent every repo of the org, so an owner proves
            # they are still an admin too.
            if not acts_as_owner or follow_id is not None:
                try:
                    still_admin = await self._attest(requester)
                except AdminAttestationUnavailable:
                    return transient_error(ADMIN_RETRY, 503)
                if not still_admin:
                    return _error(
                        "Only an admin can turn this on." if acts_as_owner else _NO_LONGER_MANAGES,
                        403,
                        retry_confirm_url,
                    )
        follow_proof: OrgProof | None = None
        if chosen is not None:
            try:
                async with self._factory() as client:
                    follow_proof = await github_connect_follow.org_proof(
                        client, token, chosen, app_jwt=app_jwt
                    )
            except (httpx.HTTPError, ValueError):
                return transient_error(_UNREACHABLE, 502)
            if follow_proof is None:
                return _error(
                    f"Only someone who sees every repo of {chosen.owner}, or an admin of "
                    f"{chosen.owner} on GitHub, can turn this on.",
                    403,
                    retry_confirm_url,
                )
        save_stage = "check_authority"
        try:
            async with self._sessionmaker.begin() as session:
                await session.execute(sql_text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                transaction_requester = await get_account_with_tenant(
                    session, account_id=invitation.requester_account_id
                )
                # The authority proved above is the one re-checked here: an owner
                # must still own the agent, never fall back to a stale stored role.
                manages = transaction_requester is not None and (
                    await self._requester_owns_agent(session, invitation, live)
                    if acts_as_owner
                    else await self._may_manage_agent(
                        session, invitation, transaction_requester, live
                    )
                )
                if not manages:
                    return _error(_NO_LONGER_MANAGES, 403)
                if follow_id is not None and (
                    transaction_requester is None or transaction_requester.role is not Role.ADMIN
                ):
                    return _error("Ask an admin to turn this on.", 403)
                save_stage = "lock_requests"
                prelocked_request_ids = await lock_confirmed_request_repos(
                    session,
                    tenant_id=invitation.tenant_id,
                    approved_by_account_id=invitation.requester_account_id,
                    extra_repo_ids={repo.repo_id for repo in repos},
                )
                save_stage = "confirm"
                saved = await github_connect.confirm(
                    session,
                    state=state,
                    cookie=cookie,
                    github_user_id=github_user_id,
                    repos=repos,
                    verified_platform_admin=still_admin,
                    requester_manages_agent=manages,
                    expected_agent_snapshot=snapshot,
                )
                if saved:
                    save_stage = "record_installations"
                    await _record_installations(session, details, repos, observed_refreshes)
                    save_stage = "remove_grants"
                    for repo_id in removed_ids:
                        await github_access.remove_grant(
                            session,
                            tenant_id=invitation.tenant_id,
                            agent_id=agent_id,
                            repo_id=repo_id,
                            changed_by_account_id=invitation.requester_account_id,
                        )
                    if repos:
                        save_stage = "check_agent"
                        await github_connect.require_app_eligible_agent(
                            session,
                            tenant_id=invitation.tenant_id,
                            agent_id=agent_id,
                            agent_name=invitation.agent_name or "",
                        )
                        existing_grants = {
                            grant.repo_id: grant
                            for grant in await github_access.list_agent_grants(
                                session, tenant_id=invitation.tenant_id, agent_id=agent_id
                            )
                        }
                        for repo in repos:
                            existing = existing_grants.get(repo.repo_id)
                            save_stage = "stage_grant"
                            await github_access.stage_grant(
                                session,
                                tenant_id=invitation.tenant_id,
                                agent_id=agent_id,
                                repo_id=repo.repo_id,
                                baseline_access="write",
                                ceiling_access="write",
                                granted_by_account_id=invitation.requester_account_id,
                                mount_path=existing.mount_path if existing else None,
                                is_working_repo=existing.is_working_repo if existing else False,
                                agent_name=invitation.agent_name,
                                repo_lock_held=True,
                            )
                    save_stage = "set_working_repo"
                    if working_choice != "keep":
                        selected_name = (
                            None if working_id is None else visible[working_id].full_name
                        )
                        await github_access.set_working_repo(
                            session,
                            tenant_id=invitation.tenant_id,
                            agent_id=agent_id,
                            repo_name=selected_name,
                            account_id=invitation.requester_account_id,
                            agent_name=invitation.agent_name,
                        )
                    elif repos and not old:
                        # Only an agent's first repos pick a working repo; later saves
                        # keep its choice, none included, so none is ever picked by chance.
                        await github_access.set_working_repo(
                            session,
                            tenant_id=invitation.tenant_id,
                            agent_id=agent_id,
                            repo_name=repos[0].full_name,
                            account_id=invitation.requester_account_id,
                            agent_name=invitation.agent_name,
                        )
                    if repos:
                        save_stage = "finish_requests"
                        resumed_requests = await finish_confirmed_requests(
                            session,
                            tenant_id=invitation.tenant_id,
                            approved_by_account_id=invitation.requester_account_id,
                            prelocked_request_ids=prelocked_request_ids,
                        )
                        if not resumed_requests:
                            await github_connect.queue_connect_followup(
                                session, invitation=invitation, repos=repos
                            )
                    if follow_id is not None and invitation.agent_ma_id is not None:
                        save_stage = "follow_org"
                        await set_agent_feature(
                            session,
                            self._settings,
                            tenant_id=invitation.tenant_id,
                            platform=requester.platform,
                            ma_agent_id=invitation.agent_ma_id,
                            key=FOLLOW_ORG_FEATURE,
                            value="on",
                            account_id=invitation.requester_account_id,
                            surface="web",
                        )
                        _log.info(
                            "github_connect.follow_turned_on",
                            extra={"proof": follow_proof, "installation_id": follow_id},
                        )
                        # Read the org now, so the gap is filled without waiting an hour.
                        await enqueue_reconciliation(
                            session,
                            installation_id=follow_id,
                            delivery_id=f"follow:{uuid.uuid4()}",
                            event="follow",
                            deleted=False,
                            now=datetime.now(UTC),
                        )
                    save_stage = "audit"
                    await append_event(
                        session,
                        tenant_id=invitation.tenant_id,
                        account_id=invitation.requester_account_id,
                        agent_id=agent_id,
                        platform=requester.platform,
                        platform_user_id=requester.platform_user_id,
                        tool_name="github_connect",
                        operation="github_connect",
                        outcome="allowed",
                        reason="agent repos saved"
                        + (f"; followed the org, proved by {follow_proof}" if follow_proof else ""),
                        github_repo_ids=selected_ids + removed_ids,
                    )
        except (
            github_connect.StaleAgentReposError,
            github_app_installations.StaleInstallationSnapshotError,
        ):
            return _error(_CHANGED, 409, retry_confirm_url)
        except github_access.InstallationLimitError as exc:
            return _error(
                str(exc),
                400,
                retry_confirm_url,
                action="Go back, remove the repos from at least one account, then save again.",
            )
        except (
            github_connect.GrantReassignmentError,
            github_access.SessionVaultBudgetError,
            github_access.ReadOnlyMultiInstallationError,
        ) as exc:
            return _error(str(exc), 400, retry_confirm_url)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in ("40001", "40P01"):
                return _error(_CHANGED, 409, retry_confirm_url)
            raise
        except github_connect.PinnedAgentConnectionError:
            return _error(github_connect.PINNED_AGENT_MESSAGE)
        except ValueError as exc:
            # Log only bounded diagnostic fields; exceptions can contain repo
            # names, credentials, or other private request data.
            _log.error(
                "github_connect.save_failed",
                extra={"stage": save_stage, "error_type": type(exc).__name__},
            )
            return _error(
                "Your changes weren't saved.",
                500,
                action="Return to your chat and ask an admin for help.",
            )
        if not saved:
            used = await self._successful_view(state, cookie, invitation_hash)
            return _page(used) if used is not None else _error()
        await self._revoke_user_token(token)
        saved_view = await self._successful_view(
            state, cookie, visible_ids=set(visible), installations=installations
        )
        if saved_view is None:
            return _error()
        return _page(saved_view, _flow_cookie(root, cookie, SAVED_COOKIE_MAX_AGE))


async def _record_installations(
    session: AsyncSession,
    details: Sequence[AppInstallationDetails],
    repos: Sequence[github_connect.RepoConfirmation],
    observed_refreshes: dict[int, datetime | None],
) -> None:
    """Record the App installations a save verified, and queue their reconciliation."""
    for detail in sorted(details, key=lambda item: item.installation_id):
        await github_app_installations.upsert_github_app(
            session,
            installation_id=detail.installation_id,
            account_id=detail.account_id,
            account_login=detail.account_login,
            account_type=detail.account_type,
            repository_selection=detail.repository_selection,
            suspended_at=detail.suspended_at,
            freshly_verified_repos=github_app_installations.VerifiedInstallationRepos(
                names=[
                    repo.full_name
                    for repo in repos
                    if repo.installation_id == detail.installation_id
                ],
                refreshed_at=observed_refreshes[detail.installation_id],
            ),
        )
        await enqueue_reconciliation(
            session,
            installation_id=detail.installation_id,
            delivery_id=f"connect:{uuid.uuid4()}",
            event="connect",
            deleted=False,
            now=datetime.now(UTC),
        )

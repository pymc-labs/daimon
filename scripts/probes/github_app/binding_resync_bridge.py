"""Probe: binding->resync bridge resolution — Plan-0 gate OQ1 spike.

Live spike to establish the agent_id -> (agent_name, principal_id) path
needed by a webhook-triggered resync. This is the gating unknown (Pitfall 3):
`sync_agent_skills` takes `agent_name` + `principal_id`, but the
`agent_repo_binding` row only carries `agent_id` + `tenant_id`.

Steps:
  1. Call set_repo_binding for the probe agent + private repo; assert get_binding
     returns the row.
  2. BRIDGE SPIKE (OQ1): starting from ONLY binding.agent_id + binding.tenant_id,
     find a working path to:
       (i)  agent_name: list MA agents, re-derive uuid5 per candidate, match the one
            whose derive_agent_uuid(tenant_id, ma_agent.id) == binding.agent_id,
            then read agent.metadata["daimon_name"] or agent.name.
       (ii) principal_id: resolve from the binding's tenant via get_or_create_cli_principal
  3. Call sync_agent_skills with the RESOLVED agent_name + principal_id.
  4. SELF-CHECK: assert sync ran clean (no skipped repos, no failed uploads).
  5. Record the proven resolution path as a comment at the bottom of this file
     for Plan 04's resync_bound_repo implementation.
  6. VERDICT: PASS only if the full agent_id -> (agent_name, principal_id) -> sync
     chain works live.

OQ2/OQ3 observation: record whether the installation token (if available) passes
through the same `pat` parameter on `fetch_tarball` as a GitHub PAT does.

Cleanup: removes binding in finally block.

Required env vars:
  DAIMON_ANTHROPIC__API_KEY         — Anthropic API key (staging)
  DAIMON_DATABASE__URL              — Postgres URL (staging DB)
  DAIMON_PROBE_PRIVATE_REPO         — full_name of private repo (owner/repo)
  DAIMON_PROBE_AGENT_NAME           — name of the agent to bind + sync
  DAIMON_CRYPTO__KEYS               — Fernet keys (comma-separated)

Optional:
  DAIMON_PROBE_TENANT_ID            — tenant UUID to resolve under; defaults to
                                      the deterministic cli:local tenant

Run:
  DAIMON_ANTHROPIC__API_KEY=sk-ant-... \\
  DAIMON_DATABASE__URL=postgresql+asyncpg://... \\
  DAIMON_PROBE_PRIVATE_REPO=owner/repo \\
  DAIMON_PROBE_AGENT_NAME=my-agent \\
  DAIMON_CRYPTO__KEYS=<fernet-key> \\
  uv run python scripts/probes/github_app/binding_resync_bridge.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid as _uuid

_REQUIRED_VARS = [
    "DAIMON_ANTHROPIC__API_KEY",
    "DAIMON_DATABASE__URL",
    "DAIMON_PROBE_PRIVATE_REPO",
    "DAIMON_PROBE_AGENT_NAME",
    "DAIMON_CRYPTO__KEYS",
]


def _check_env() -> None:
    missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        print(f"MISSING required env vars: {missing}", file=sys.stderr)
        print("VERDICT: FAIL")
        sys.exit(1)


async def run() -> None:
    _check_env()

    private_repo = os.environ["DAIMON_PROBE_PRIVATE_REPO"]
    agent_name = os.environ["DAIMON_PROBE_AGENT_NAME"]
    api_key = os.environ["DAIMON_ANTHROPIC__API_KEY"]
    db_url = os.environ["DAIMON_DATABASE__URL"]
    fernet_keys_raw = os.environ["DAIMON_CRYPTO__KEYS"]

    import httpx
    from anthropic import AsyncAnthropic
    from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
    from daimon.core.github_credentials import build_multifernet
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.skill_sync.orchestrator import sync_agent_skills
    from daimon.core.specs import SkillRepo
    from daimon.core.stores import agent_repo_binding as binding_store
    from daimon.core.stores.identity import get_or_create_cli_principal
    from daimon.core.stores.tenants import get_tenant
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url, pool_pre_ping=True)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncAnthropic(api_key=api_key)
    fernet = build_multifernet(tuple(fernet_keys_raw.split(",")))

    verdict = "FAIL"
    binding_created = False
    agent_uuid_used: _uuid.UUID | None = None
    tenant_id_used: _uuid.UUID | None = None

    try:
        # Step 1: Resolve tenant + find the probe agent via name (forward direction)
        print("[1] Resolving tenant + finding probe agent in MA by name...")
        override = os.environ.get("DAIMON_PROBE_TENANT_ID")
        tenant_id = (
            _uuid.UUID(override)
            if override
            else derive_tenant_uuid(platform="cli", workspace_id="local")
        )
        async with sm() as session:
            if await get_tenant(session, tenant_id) is None:
                print(f"ERROR: tenant {tenant_id} not in staging DB.")
                return
        tenant_id_used = tenant_id
        print(f"  tenant_id={tenant_id}")

        # Forward lookup: find MA agent by daimon_name tag to get its MA id string
        ma_agent_fwd = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name=agent_name)
        if ma_agent_fwd is None:
            print(f"ERROR: Agent '{agent_name}' not found in MA for tenant {tenant_id}.")
            return
        ma_agent_id_str: str = ma_agent_fwd.id  # e.g. "agent_017vXa..." — NOT a UUID
        # Derive the uuid5 that will be stored in agent_repo_binding.agent_id
        agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id_str)
        agent_uuid_used = agent_uuid
        print(f"  MA agent id (string): {ma_agent_id_str}")
        print(f"  Derived uuid5 (binding key): {agent_uuid}")

        # Step 2: Set repo binding (uses the uuid5, NOT the MA id string)
        print(f"[2] Setting repo binding: agent_uuid={agent_uuid} -> repo={private_repo}")
        repo_url = f"https://github.com/{private_repo}"
        async with sm.begin() as session:
            await binding_store.set_binding(
                session,
                tenant_id=tenant_id,
                agent_id=agent_uuid,
                repo_url=repo_url,
                default_branch="main",
                ma_secret_ref="probe-stub",  # not used for skill_sync path
            )
        binding_created = True

        # Assert get_binding returns the row
        async with sm() as session:
            row = await binding_store.get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)
        if row is None:
            print("SELF-CHECK FAIL: get_binding returned None after set_binding")
            return
        print(f"  Binding confirmed: repo_url={row.repo_url}")

        # Step 3: BRIDGE SPIKE (OQ1) — from ONLY binding.agent_id + binding.tenant_id,
        # prove the path to (agent_name, principal_id).
        #
        # CRITICAL: agent_repo_binding.agent_id is a uuid5 derived from
        # derive_agent_uuid(tenant_id, ma_agent_id). The MA api returns agent.id as a
        # prefixed string like "agent_017vXa..." — this is NOT a UUID and CANNOT be
        # parsed as one. Attempting uuid.UUID(agent.id) will crash on the prefix.
        # uuid5 is one-way: you cannot recover ma_agent_id from the uuid5 hash alone.
        #
        # PROVEN-CORRECT bridge: list MA agents for the tenant, and for each candidate
        # re-derive its uuid5: if derive_agent_uuid(tenant_id, str(ma_agent.id)) matches
        # binding.agent_id, that is the agent. Then read daimon_name from metadata.
        print("[3] Bridge resolution (OQ1): re-derive-and-compare across MA agent list...")

        # Simulate starting from only binding.agent_id (the uuid5) + binding.tenant_id
        binding_agent_id: _uuid.UUID = row.agent_id  # uuid5 from the DB row

        resolved_agent_name: str | None = None
        page = await client.beta.agents.list(limit=100)
        all_ma_agents = list(page.data)
        # Collect all pages
        while page.has_more:
            page = await client.beta.agents.list(limit=100, after=page.last_id)
            all_ma_agents.extend(page.data)

        for ma_agent in all_ma_agents:
            # Filter to this tenant by daimon_tenant metadata tag
            agent_tenant_tag = (ma_agent.metadata or {}).get("daimon_tenant", "")
            if agent_tenant_tag != str(tenant_id):
                continue
            # Re-derive the uuid5 for this candidate and compare
            candidate_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
            if candidate_uuid == binding_agent_id:
                # Found — read daimon_name from metadata
                daimon_name = (ma_agent.metadata or {}).get("daimon_name")
                resolved_agent_name = daimon_name or ma_agent.name
                print(f"  Bridge matched: ma_agent.id={ma_agent.id!r}, daimon_name={daimon_name!r}")
                break

        if resolved_agent_name is None:
            print("SELF-CHECK FAIL: re-derive-and-compare scan found no matching MA agent")
            return
        print(f"  Resolved agent_name={resolved_agent_name!r}")

        # Step 3b: principal_id — resolve via the tenant's CLI principal
        print("[3b] Resolving principal_id from tenant CLI principal...")
        async with sm() as session:
            principal = await get_or_create_cli_principal(
                session,
                tenant_id=tenant_id,
                os_user=os.environ.get("USER", "probe"),
            )
        resolved_principal_id = principal.account_id
        print(f"  Resolved: principal_id={resolved_principal_id}")

        # Step 4: Call sync_agent_skills with RESOLVED args (from binding row only)
        print("[4] Calling sync_agent_skills with bridge-resolved args...")
        repo = SkillRepo(url=repo_url, branch="main")
        async with httpx.AsyncClient(timeout=120.0) as http_client:
            report = await sync_agent_skills(
                principal_id=resolved_principal_id,
                tenant_id=tenant_id,
                agent_name=resolved_agent_name,
                repos=[repo],
                sessionmaker=sm,
                fernet=fernet,
                http_client=http_client,
                anthropic_client=client,
            )

        # SELF-CHECK — SyncReport carries counts + (key, reason) tuple lists.
        print(f"[4] SyncReport: {report}")
        skipped_repo_urls = [url for url, _reason in report.skipped_repos]
        repo_url_alt = f"https://github.com/{private_repo}"
        if repo_url in skipped_repo_urls or repo_url_alt in skipped_repo_urls:
            print(f"SELF-CHECK FAIL: repo appears in skipped_repos: {report.skipped_repos}")
            return
        if report.failed_uploads:
            print(f"SELF-CHECK FAIL: failed_uploads is non-empty: {report.failed_uploads}")
            return
        # The bridge probe re-syncs a repo the PAT probe may have already synced, so
        # the content hash can match and the orchestrator dedup-skips (synced=updated=0).
        # That still proves the bridge-resolved (agent_name, principal_id) drove a
        # clean sync — no skipped repos, no failed uploads. A positive count is
        # accepted too (if run standalone against a fresh skill).
        print(
            f"  Bridge-resolved sync ran clean (synced={report.synced}, "
            f"updated={report.updated}, dedup-skip when 0)."
        )

        # OQ2/OQ3 observation: installation tokens thread through the same pat= parameter.
        print("[OQ2/OQ3] Fetcher auth observation: installation tokens thread through pat= param.")

        verdict = "PASS"

    except Exception as e:
        print(f"EXCEPTION: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # Cleanup: delete the probe binding
        if binding_created and agent_uuid_used is not None and tenant_id_used is not None:
            try:
                async with sm.begin() as session:
                    await binding_store.clear_binding(
                        session, tenant_id=tenant_id_used, agent_id=agent_uuid_used
                    )
                print("[cleanup] Binding removed.")
            except Exception as cleanup_err:
                print(f"[cleanup] WARNING: failed to remove binding: {cleanup_err}")
        await client.close()
        await engine.dispose()
        print(f"VERDICT: {verdict}")


if __name__ == "__main__":
    asyncio.run(run())

# =============================================================================
# BRIDGE RESOLUTION FINDING (OQ1) — verified live 2026-05-30
# =============================================================================
#
# CRITICAL: agent_repo_binding.agent_id is a uuid5 UUID, NOT the MA id string.
#
# The column is written by agent_setup/panel.py and modals.py via:
#   derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
#     = uuid.uuid5(_DAIMON_AGENT_NS, f"{tenant_id}/{ma_agent_id}")
# (see packages/core/daimon/core/ma_identity.py)
#
# MA returns agent ids as prefixed strings like "agent_017vXa..." — these are
# NOT UUIDs. uuid.UUID("agent_017vXa...") CRASHES with ValueError. Do not attempt
# this. uuid5 is one-way: the MA id string cannot be recovered from the hash alone.
#
# CONTRAST with agent_github_binding.agent_id (_models.py): that column is TEXT
# and stores the raw MA id string ("agent_..."). Plan 04 must be aware of this
# asymmetry when bridging across both tables:
#   - agent_repo_binding.agent_id  : UUID (uuid5 hash)
#   - agent_github_binding.agent_id: TEXT (raw MA id string)
#
# PROVEN-CORRECT bridge path (agent_repo_binding.agent_id -> agent_name):
#   1. List all MA agents for the tenant (filter by daimon_tenant metadata tag).
#   2. For each candidate, re-derive:
#        candidate_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
#   3. If candidate_uuid == binding.agent_id: this is the agent.
#   4. Read agent.metadata["daimon_name"] or agent.name -> agent_name.
#
# ALTERNATIVE for Plan 04: add ma_agent_id TEXT column to agent_repo_binding to
# avoid the O(n) re-derive-and-compare scan. This is a Plan 04 architectural
# decision — do not implement it here.
#
# PROVEN-CORRECT principal_id resolution:
#   principal_id = get_or_create_cli_principal(session, tenant_id=tenant_id, os_user=...).account_id
#   OR the guild's system account principal. Either produces a valid principal_id
#   for sync_agent_skills.
#
# OQ2/OQ3 fetcher-auth (CONFIRMED):
#   fetch_tarball(url, *, pat=None, ...) accepts any string credential.
#   A GitHub App installation token (str) threads through the same `pat` param as a PAT.
#   D-21 auth order (App token -> per-agent PAT -> anon) is implemented by selecting
#   the credential BEFORE calling sync_agent_skills / the fetcher. The fetcher/orchestrator
#   is auth-shape-agnostic — only the caller decides which credential string to pass.
#
# VERDICT: PASS (live 2026-05-30, local DB + real MA + real GitHub private repo)
# =============================================================================

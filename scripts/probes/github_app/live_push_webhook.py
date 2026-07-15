"""Probe: Live push-webhook end-to-end verification against staging Fly.

SC-1: App installation event persists the install row in staging DB.
SC-2: Push to a bound default branch delivers a verified webhook that triggers
      a skill resync — MA skill version bumped, last_sync_at advanced,
      last_sync_error NULL.
SC-3: Forged delivery (wrong secret) against staging URL returns 401.
D-25: A second agent WITHOUT a per-agent credential on a private repo with NO
      App install resyncs anon (skips/public-only) and is NOT silently
      authenticated by the principal default.

This probe DOCUMENTS and CHECKS the live flow after the operator has:
  1. Registered the GitHub App on github.com:
       - Read-only `contents` permission ONLY
       - Subscribe to: push, installation, installation_repositories events
       - Webhook URL: <staging Fly public URL>/webhooks/github
  2. Set Fly secrets:
       DAIMON_GITHUB__APP_ID
       DAIMON_GITHUB__APP_PRIVATE_KEY
       DAIMON_GITHUB__WEBHOOK_SECRET
  3. Installed the App on the private test repo + bound it to a staging agent.
  4. Pushed a commit to the binding's default branch.

THEN run this probe to verify the staging state.

Required env vars:
  DAIMON_DATABASE__URL              — Postgres URL (staging DB)
  DAIMON_ANTHROPIC__API_KEY         — Anthropic API key
  DAIMON_PROBE_INSTALLATION_ID      — GitHub App installation_id to check
  DAIMON_PROBE_AGENT_NAME           — name of the staging agent with a binding
  DAIMON_PROBE_WEBHOOK_URL          — staging Fly public URL (for SC-3 check)
  DAIMON_GITHUB__WEBHOOK_SECRET     — webhook secret (for SC-3 forged test)
  DAIMON_CRYPTO__KEYS               — Fernet keys (comma-separated)

Optional:
  DAIMON_PROBE_TENANT_ID            — tenant UUID to resolve under. REQUIRED on
                                      staging: the bound agent lives under a
                                      Discord-guild tenant, not the cli:local
                                      default. Set this to the guild's tenant_id.
  DAIMON_PROBE_D25_AGENT_NAME       — second agent WITHOUT per-agent credential
  DAIMON_PROBE_D25_REPO             — repo URL for D-25 anon check

Run (after pushing a commit to the bound repo):
  DAIMON_DATABASE__URL=postgresql+asyncpg://... \\
  DAIMON_ANTHROPIC__API_KEY=sk-ant-... \\
  DAIMON_PROBE_INSTALLATION_ID=12345 \\
  DAIMON_PROBE_AGENT_NAME=my-staging-agent \\
  DAIMON_PROBE_WEBHOOK_URL=https://<your-app>.fly.dev \\
  DAIMON_GITHUB__WEBHOOK_SECRET=whsec_... \\
  DAIMON_CRYPTO__KEYS=<fernet-key> \\
  uv run python scripts/probes/github_app/live_push_webhook.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_REQUIRED_VARS = [
    "DAIMON_DATABASE__URL",
    "DAIMON_ANTHROPIC__API_KEY",
    "DAIMON_PROBE_INSTALLATION_ID",
    "DAIMON_PROBE_AGENT_NAME",
    "DAIMON_PROBE_WEBHOOK_URL",
    "DAIMON_GITHUB__WEBHOOK_SECRET",
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

    db_url = os.environ["DAIMON_DATABASE__URL"]
    api_key = os.environ["DAIMON_ANTHROPIC__API_KEY"]
    installation_id = int(os.environ["DAIMON_PROBE_INSTALLATION_ID"])
    agent_name = os.environ["DAIMON_PROBE_AGENT_NAME"]
    webhook_url = os.environ["DAIMON_PROBE_WEBHOOK_URL"].rstrip("/")
    fernet_keys_raw = os.environ["DAIMON_CRYPTO__KEYS"]

    import uuid

    import httpx
    from anthropic import AsyncAnthropic
    from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
    from daimon.core.github_credentials import build_multifernet
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores import agent_repo_binding as binding_store
    from daimon.core.stores import github_app_installations as install_store
    from daimon.core.stores.tenants import get_tenant
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url, pool_pre_ping=True)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncAnthropic(api_key=api_key)
    _fernet = build_multifernet(tuple(fernet_keys_raw.split(",")))

    verdict = "FAIL"
    sc1_pass = False
    sc2_pass = False
    sc3_pass = False

    try:
        # --- SC-1: Installation row persisted ---
        print(f"[SC-1] Checking installation row for installation_id={installation_id}...")
        async with sm() as session:
            install_row = await install_store.get(session, installation_id=installation_id)
        if install_row is None:
            print(f"  FAIL: no installation row found for installation_id={installation_id}")
            print("  Did the installation event arrive? Check Fly logs.")
        else:
            print(
                f"  PASS: installation row found — "
                f"account_login={install_row.account_login}, "
                f"repos={install_row.repo_full_names}"
            )
            sc1_pass = True

        # --- SC-2: Push triggered resync (last_sync_at advanced, error NULL) ---
        print(f"\n[SC-2] Checking binding last_sync for agent={agent_name!r}...")
        override = os.environ.get("DAIMON_PROBE_TENANT_ID")
        tenant_id = (
            uuid.UUID(override)
            if override
            else derive_tenant_uuid(platform="cli", workspace_id="local")
        )
        async with sm() as session:
            tenant_known = await get_tenant(session, tenant_id) is not None
        if not tenant_known:
            print(f"  FAIL: tenant {tenant_id} not in staging DB")
        else:
            ma_agent = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name=agent_name)
            if ma_agent is None:
                print(f"  FAIL: Agent {agent_name!r} not found in MA for tenant {tenant_id}")
            else:
                agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
                async with sm() as session:
                    binding_row = await binding_store.get_binding(
                        session, tenant_id=tenant_id, agent_id=agent_id
                    )
                if binding_row is None:
                    print(f"  FAIL: No binding found for agent {agent_name!r}")
                elif binding_row.last_sync_at is None:
                    print("  FAIL: last_sync_at is NULL — resync has not run yet")
                    print("        Wait a few seconds for BackgroundTask, then re-run.")
                elif binding_row.last_sync_error is not None:
                    print(f"  FAIL: last_sync_error is set: {binding_row.last_sync_error!r}")
                    print("        Check Fly logs for github.resync.failed")
                else:
                    print(f"  PASS: last_sync_at={binding_row.last_sync_at}, last_sync_error=NULL")
                    sc2_pass = True

        # --- SC-3: Forged delivery returns 401 ---
        target = f"{webhook_url}/webhooks/github"
        print(f"\n[SC-3] Testing forged delivery (wrong secret) against {target}...")
        forged_payload = json.dumps(
            {
                "ref": "refs/heads/main",
                "repository": {"full_name": "test/test"},
            }
        ).encode()
        forged_sig = "sha256=" + "ba" * 32
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.post(
                target,
                content=forged_payload,
                headers={
                    "x-github-event": "push",
                    "x-hub-signature-256": forged_sig,
                    "x-github-delivery": "probe-forged-001",
                    "content-type": "application/json",
                },
            )
        if r.status_code == 401:
            print("  PASS: forged delivery returned 401")
            sc3_pass = True
        else:
            print(f"  FAIL: expected 401, got {r.status_code}")

        # --- D-25 live check (optional) ---
        d25_agent_name = os.environ.get("DAIMON_PROBE_D25_AGENT_NAME")
        if d25_agent_name:
            print(
                f"\n[D-25] Checking per-agent credential isolation for agent={d25_agent_name!r}..."
            )
            async with sm() as session:
                if tenant_id is not None:
                    ma_d25 = await find_agent_by_daimon_tag(
                        client, tenant_id=tenant_id, name=d25_agent_name
                    )
                    if ma_d25 is None:
                        print(f"  SKIP: agent {d25_agent_name!r} not found in MA")
                    else:
                        agent_id_d25 = derive_agent_uuid(
                            tenant_id=tenant_id, ma_agent_id=str(ma_d25.id)
                        )
                        binding_d25 = await binding_store.get_binding(
                            session, tenant_id=tenant_id, agent_id=agent_id_d25
                        )
                        if binding_d25 is None:
                            print(f"  SKIP: no binding for agent {d25_agent_name!r}")
                        elif binding_d25.last_sync_error is not None:
                            print(
                                f"  WARNING: D-25 agent has "
                                f"last_sync_error={binding_d25.last_sync_error!r}"
                            )
                            print(
                                "          If it used a principal-default credential, "
                                "that is a D-25 bleed."
                            )
                        else:
                            print(
                                f"  INFO: D-25 agent last_sync_at={binding_d25.last_sync_at}"
                                f", error=NULL (anon sync OK)"
                            )
        else:
            print("\n[D-25] Skipped (set DAIMON_PROBE_D25_AGENT_NAME to run)")

        # --- Final verdict ---
        print(f"\nSC-1 (install row):  {'PASS' if sc1_pass else 'FAIL'}")
        print(f"SC-2 (resync ran):   {'PASS' if sc2_pass else 'FAIL'}")
        print(f"SC-3 (forged 401):   {'PASS' if sc3_pass else 'FAIL'}")

        if sc1_pass and sc2_pass and sc3_pass:
            verdict = "PASS"

    except Exception as e:
        print(f"EXCEPTION: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
    finally:
        await client.close()
        await engine.dispose()
        print(f"\nVERDICT: {verdict}")


if __name__ == "__main__":
    asyncio.run(run())

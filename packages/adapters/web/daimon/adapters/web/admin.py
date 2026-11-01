"""`/api/admin/*`: people, invites, agent web access, the audit log and settings.

One module per concern; every change is audited in the transaction that makes it.
"""

from __future__ import annotations

from daimon.adapters.web.admin_access import access_router
from daimon.adapters.web.admin_audit import audit_router
from daimon.adapters.web.admin_invites import invites_admin_router
from daimon.adapters.web.admin_people import people_router
from daimon.adapters.web.admin_settings import settings_router
from daimon.adapters.web.runtime import WebRuntime
from fastapi import APIRouter


def admin_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()
    for build in (
        people_router,
        invites_admin_router,
        access_router,
        audit_router,
        settings_router,
    ):
        router.include_router(build(runtime))
    return router

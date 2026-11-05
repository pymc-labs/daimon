"""The admin audit trail: one row for every change an admin makes in the web app.

The recording lives in core, shared with the chat tools.
"""

from __future__ import annotations

from daimon.core.web_admin import AuditTarget as AuditTarget
from daimon.core.web_admin import audit as audit
from daimon.core.web_admin import invite_target as invite_target
from daimon.core.web_admin import person_target as person_target
from daimon.core.web_admin import usd as usd

"""Add an audit source to the workspace trail."""

from daimon.core.audit_trail import (
    AuditEntry,
    AuditFilter,
    AuditSourceSpec,
    SourceAuditRow,
    register_audit_source,
)

__all__ = [
    "AuditEntry",
    "AuditFilter",
    "AuditSourceSpec",
    "SourceAuditRow",
    "register_audit_source",
]

"""The one form of a source principal, shared by the data store and core's readers, and the
scope everyone may read."""

from daimon.core.data_audience import PUBLIC, subject, user_subject

__all__ = ["PUBLIC", "subject", "user_subject"]

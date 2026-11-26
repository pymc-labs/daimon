"""Identity keys stamped on every Daimon resource's metadata.

A leaf module: record stores import these without loading `daimon.core.defaults`,
whose package init reaches back into the stores.
"""

MA_METADATA_KEY_TENANT = "daimon_tenant"
MA_METADATA_KEY_NAME = "daimon_name"
MA_METADATA_KEY_ACCOUNT = "daimon_account"
MA_METADATA_KEY_MANAGED = "daimon_managed"
#: The signed grant (`chat_session_id`) of the Daimon credential a session mounts.
MA_METADATA_KEY_CHAT_SESSION = "daimon_chat_session_id"

"""Identity keys stamped on every Daimon resource's metadata.

A leaf module: record stores import these without loading `daimon.core.defaults`,
whose package init reaches back into the stores.
"""

MA_METADATA_KEY_TENANT = "daimon_tenant"
MA_METADATA_KEY_NAME = "daimon_name"
MA_METADATA_KEY_ACCOUNT = "daimon_account"

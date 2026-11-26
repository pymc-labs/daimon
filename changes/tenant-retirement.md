- **A workspace uninstalled for 30 days is retired.**
  Its data, provider sessions, environments, memory, credentials and their
  vaults, skills, reports and notebooks are deleted in short batches that an
  interrupted run resumes, and its provider agents are archived. Billing, usage
  and support-credit records stay without the person they came from, so a
  reinstall keeps its balance and gets no second trial or support credits; a
  reinstall during a retirement stops it and keeps what was not yet deleted.
  `daimon tenants retire` retires one at once; it and `daimon tenants delete`
  refuse while usage is still settling unless `--force` is given, which leaves
  that usage unbilled.

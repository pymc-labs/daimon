- **The report and notebook hosts keep less, and send fewer details out.**
  Reports are deleted 365 days after their last update
  (`DAIMON_REPORT__RETENTION_DAYS`, `0` keeps them), and both hosts take an
  admin call that deletes one workspace's reports or notebooks. The report
  viewer serves pdf.js from the host instead of a public CDN, report pages carry
  a content security policy, both hosts send `Referrer-Policy: no-referrer`, and
  access logs no longer record the client address.

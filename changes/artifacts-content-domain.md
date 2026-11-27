- **Artifact pages can run on their own domain and ask before acting.**
  `DAIMON_WEB__ARTIFACT_CONTENT_URL` serves each page from its own origin,
  where it keeps storage and embeds YouTube and Google Drive players. Pages
  open no tabs themselves: Daimon asks the viewer before following a link to
  another site. A page's first question to the agent on each version needs
  the viewer's yes, which they can take back. Refused pages say why, keep the
  `#section` through sign-in, and `daimon.me()` includes the viewer's email.
  The list and chat cards show version thumbnails.
  `DAIMON_WEB__ARTIFACT_ASSET_HOSTS`, `DAIMON_WEB__FORWARDED_ALLOW_IPS` and
  `DAIMON_WEB__ARTIFACT_DB_POOL_SIZE` are new; `res.cloudinary.com` is no
  longer an allowed asset host.

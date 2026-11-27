- **Page assets from one path of a host.** `DAIMON_WEB__ARTIFACT_ASSET_HOSTS` takes an
  exact https host with a path ending in `/` (`https://img.example.com/account/upload/`),
  so pages load only that service's own files. A whole host that fetches any URL is
  still refused, wildcard hosts take no path, and `%` and dot segments are rejected.

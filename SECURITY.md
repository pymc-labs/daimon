# Security Policy

daimon deployments hold sensitive operator credentials — an Anthropic API
key and platform bot tokens (Discord, Slack, etc.) — on behalf of every
guild/workspace the bot serves. A vulnerability in this project can expose
those credentials or let one tenant access another tenant's data, so we take
reports seriously.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for security vulnerabilities.

Instead, use GitHub's private vulnerability reporting: go to the
**Security** tab of this repository and select **Report a vulnerability**.
This opens a private conversation with the maintainers where you can share
details, reproduction steps, and impact without disclosing the issue
publicly.

## Supported versions

This project is pre-1.0. Only the latest commit on `main` is supported;
please make sure you can reproduce the issue there before reporting.

## Response expectations

We aim to acknowledge new reports within a few business days and will keep
you updated as we investigate and work on a fix. Coordinated disclosure
timing is worked out with the reporter once a fix is available.

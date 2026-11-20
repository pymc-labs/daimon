## Summary

<!-- What does this PR change, and why? -->

## Checklist

- [ ] Tests added or updated for any behavior change
- [ ] New migrations declare `schema:` and pass `scripts/rechain_migrations.py --check`
- [ ] Every path that does this behaviour is changed, or the difference is recorded in a test (CLAUDE.md, "One rule, every path")
- [ ] `uv run pytest` passes locally
- [ ] `uv run pyright` passes locally (strict mode)
- [ ] `uv run ruff check .` is clean
- [ ] `uv run lint-imports` passes (package boundary contracts)

## Summary

<!-- What does this PR change, and why? -->

## Checklist

- [ ] Tests added or updated for any behavior change
- [ ] Every path that does this behaviour is changed, or the difference is recorded in a test (CLAUDE.md, "One rule, every path")
- [ ] `uv run pytest` passes locally
- [ ] `uv run pyright` passes locally (strict mode)
- [ ] `uv run ruff check .` is clean
- [ ] `uv run lint-imports` passes (package boundary contracts)
- [ ] Docs: a changed fact, command, limit or feature page is fixed in `docs/` (web-app text inside `<!-- knowledge: web -->` markers); a new feature sets `page=`

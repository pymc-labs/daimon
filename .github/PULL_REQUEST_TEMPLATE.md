## Summary

<!-- What does this PR change, and why? -->

## Checklist

- [ ] Tests added or updated for any behavior change
- [ ] New migrations declare `schema:` and pass `scripts/rechain_migrations.py --check`
- [ ] Every path that does this behaviour is changed, or the difference is recorded in a test (CLAUDE.md, "One rule, every path")
- [ ] Each changed path a person takes is dumb simple: listed from start to result, and taken in the app or marked not taken (CLAUDE.md, "Make it dumb simple")
- [ ] `uv run pytest` passes locally
- [ ] `uv run pyright` passes locally (strict mode)
- [ ] `uv run ruff check .` is clean
- [ ] `uv run lint-imports` passes (package boundary contracts)
- [ ] Docs: a changed fact, command, limit or feature page is fixed in `docs/` (web-app text inside `<!-- knowledge: web -->` markers); a new feature sets `page=`
- [ ] A user-visible change has a `changes/` fragment

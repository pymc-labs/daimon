## Summary

<!-- What does this PR change, and why? -->

## Checklist

- [ ] Tests added or updated for any behavior change
- [ ] Every path that does this behaviour is changed, or the difference is recorded in a test (CLAUDE.md, "One rule, every path")
- [ ] Each changed path a person takes is dumb simple: checked in the app from its start to its result (CLAUDE.md, "Make it dumb simple")
- [ ] `uv run pytest` passes locally
- [ ] `uv run pyright` passes locally (strict mode)
- [ ] `uv run ruff check .` is clean
- [ ] `uv run lint-imports` passes (package boundary contracts)

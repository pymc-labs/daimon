# Changelog fragments

A change a user can notice adds one file here, `changes/<topic>.md`, holding one
paragraph in the style of `CHANGELOG.md`:

```
- **What changed, as the person sees it.** One to three sentences on the
  behaviour and any default, wrapped at 80 columns with a two-space indent.
```

Never edit `CHANGELOG.md` in a change: two branches that both added a line
there conflicted on every merge. `scripts/changelog.py check` (CI) validates
the fragments; `scripts/changelog.py fold [--until SHA]` moves them into
`[Unreleased]`, newest first, and removes them.

# Agent face catalogue

`manifest.json` is the draw table for the default agent faces. Each colour and
layer has a stable `id`, a `weight`, and a `retired` flag. Layer entries also name
their PNG and placement. The renderer bundles these files with `daimon-core`.

The avatar row stores a variant made of seven IDs: background, eyes, mouth, hat,
eyewear, brows, and base. It also stores the rendered PNG and a compact 20 px
thumbnail used when assigning other agents. Posting reuses that row;
uploading a picture retains the variant; Reset renders the same variant again.
The identity switch must be on for a new default face to be assigned.
The first turn uses the current picture or none and queues missing artwork in a
background database session. An initials URL remains valid after the new face
is stored, until an admin changes or resets the picture.

## Edit the catalogue

- Change `weight` to change future draws. Larger weights are drawn more often.
  Zero weight excludes an entry from new draws. The `draw` section controls the
  overall hat and shades shares and the target share of each colour group. Colour
  assignments also spread hues against faces already in the workspace.
- Add a colour with a new ID and hex value. To change an existing colour, add
  the replacement under a new ID and retire the old entry. Its old hex value
  must remain so stored variants render as before.
- Add a PNG under a new filename and add a layer entry with a new ID, kind,
  placement, weight, its `sha256`, and `retired: false`. Get the hash with
  `sha256sum path/to/layer.png`. Keep the image at 1024 px, on a
  transparent canvas, aligned to the current face. Set `retired: true` on an
  older layer to stop drawing it for new agents. Keep its entry and PNG: stored
  variants still need both. Do not replace the bytes of an existing layer; the
  package refuses to load a file whose hash differs from the manifest.
- Keep the built-in face's entries in `classic`. The built-in Daimon continues
  to use the platform app picture; these entries reserve its colours and shape
  when assigning other faces. Keep `hat-none` and `eyewear-none` active: they
  represent the common no-prop variants.

The list order is part of the draw for new agents. Existing rows use IDs rather
than list positions, so reordering a list or changing weights does not reassign
them. A future admin re-roll action can explicitly select a new variant; Reset
does not do that.

Run the focused test after each edit:

```bash
uv run pytest -n 2 packages/core/tests/test_agent_faces.py
```

Render the pick sheet and the Slack thread preview from the shipped code:

```bash
uv run python scripts/render_agent_face_sheet.py
uv run python scripts/render_agent_face_slack_mock.py
uv run python scripts/render_agent_face_hat_sheet.py
```

The output files are `docs/assets/agent-faces-50.png`,
`docs/assets/agent-face-slack-thread.png`, and
`docs/assets/agent-face-hats.png`. The hat sheet shows 512 px and actual-size
36 px square and rounded-square headers. The mock renders agents at 36 px and
uses the classic generated face for the built-in app. Pass
`--built-in-image PATH` to use a platform's current app picture, and
`--output PATH` to write another copy.

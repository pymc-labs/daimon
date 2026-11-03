# Slides requests

Envelope: `{"operation":"...","deck_id":"EXPLICIT_ID","body":{...}}`.
Only `copy-template` omits deck_id; its returned ID is the target for later calls.
There is no default deck or folder configuration.

| Operation | Body |
| --- | --- |
| `copy-template` | `template_id`, `title`, optional `folder_id` (no sharing change) |
| `read` | `{}`; returns slide elements and page size |
| `add-slide` | `duplicate_slide_id` to preserve styling, or `{}` for a blank slide |
| `set-text` | `texts`: shape ID → full replacement string (empty clears) |
| `replace-text` | `replacements`: literal search → replacement, optional `slide_ids` |
| `add-text-box` | `slide_id`, `text`, `x`, `y`, `width`, `height` in points |
| `add-image` | same geometry with public HTTPS `url` instead of text |
| `delete` | `object_ids`: slide or shape/element IDs; must retain one slide |
| `batch` | `requests`: Google Slides API request array, one type per object |
| `render` | `slide_ids`: explicit IDs to render to PNG |

Batch supports createSlide, duplicateObject (slide plus complete objectIds map),
createShape, createImage, insertText, deleteText, replaceAllText, deleteObject;
updateTextStyle, updateParagraphStyle, createParagraphBullets,
deleteParagraphBullets, updateShapeProperties, updatePageProperties,
updatePageElementTransform, updateImageProperties, updateLineProperties,
updatePageElementsZOrder, updateSlidesPosition. Unknown request types are refused.
Raw batch geometry follows the Slides API's size/transform units; high-level
geometry is in points. For grouped shapes, transforms are relative to the parent.
Delete group children individually. Batch simulation checks IDs, deletion order
and the final-slide rule, including objects created within that batch. Writes
carry the deck's required revision to refuse concurrent changes.

```json
{"operation":"replace-text","deck_id":"EXPLICIT_DECK_ID","body":{"replacements":{"{{CLIENT}}":"Example"},"slide_ids":["EXPLICIT_SLIDE_ID"]}}
```

Read the deck afterward to detect zero-match replacements and confirm exact text.
Copies default explicitly to the shared account’s My Drive, never the template
folder; a named destination folder can inherit its access. Do not
silently share; report the link and any access limitation. Bound Google scopes
include full `https://www.googleapis.com/auth/drive` for existing templates and
`https://www.googleapis.com/auth/presentations` for deck edits and thumbnails.

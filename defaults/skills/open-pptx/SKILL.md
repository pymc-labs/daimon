---
name: open-pptx
description: Create, read and edit PowerPoint (.pptx) files with python-pptx. Use for any .pptx request: a deck to build, or an existing presentation to read or change.
---

# PowerPoint decks

Use `python-pptx` (`from pptx import Presentation`). If the import fails, run
`python3 -m pip install python-pptx`.

## Read

```python
from pptx import Presentation
prs = Presentation("in.pptx")
for i, slide in enumerate(prs.slides, 1):
    for shape in slide.shapes:
        if shape.has_text_frame: print(i, shape.text_frame.text)
    if slide.has_notes_slide: print(i, "notes:", slide.notes_slide.notes_text_frame.text)
```

## Create

Start from `Presentation()` (or the user's template) and use its layouts:
`prs.slides.add_slide(prs.slide_layouts[1])`, then fill `slide.shapes.title` and
`slide.placeholders[1]`. Add pictures with `slide.shapes.add_picture(path, left,
top, width=Inches(6))` and charts with `slide.shapes.add_chart`. One idea per
slide, short text, charts made with matplotlib and inserted as images when a
native chart is not needed. Save with `prs.save("out.pptx")`.

## Edit

Change `run.text` inside existing text frames to keep formatting. Reorder or
delete slides through `prs.slides._sldIdLst`.

## Check

Reopen the file and list each slide's text. When LibreOffice exists, render with
`soffice --headless --convert-to pdf out.pptx` and look at the pages for text
that overflows its box.

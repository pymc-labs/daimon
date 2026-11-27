---
name: open-docx
description: Create, read and edit Word (.docx) files with python-docx. Use for any .docx request, for example a report, letter or memo to produce, or an existing document to read or change.
---

# Word documents

Use `python-docx` (`import docx`), installed in the sandbox. If the import fails and
`pip install python-docx` has no network, say the library is missing; don't hand-write the XML.

## Read

```python
from docx import Document
doc = Document("in.docx")
for p in doc.paragraphs: print(p.style.name, "|", p.text)
for t in doc.tables: print([[c.text for c in r.cells] for r in t.rows])
```

## Create

Build from built-in styles, not manual fonts: `doc.add_heading(text, level)`,
`doc.add_paragraph(text, style="List Bullet")`, `doc.add_table(rows, cols)` with
`table.style = "Table Grid"`, `doc.add_picture(path, width=Inches(6))`,
`doc.add_page_break()`. Save with `doc.save("out.docx")`.

## Edit

Open the file, change paragraphs and runs in place, and save under a new name.
Keep a paragraph's formatting by editing `run.text`, not `paragraph.text` (which
drops run styling). Headers and footers are `doc.sections[0].header`/`.footer`.

## Check

Reopen the saved file and print its text. To look at it, convert with
`soffice --headless --convert-to pdf out.docx` when LibreOffice exists. Tracked
changes and comments are not supported by python-docx: say so rather than
dropping them silently.

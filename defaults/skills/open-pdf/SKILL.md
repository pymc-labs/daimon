---
name: open-pdf
description: Read, merge, split, fill and create PDF files with pypdf, pdfplumber and reportlab. Use for any .pdf request: text or tables to extract, pages to combine or split, a form to fill, or a PDF to produce.
---

# PDF files

Libraries: `pypdf` (pages, merge, split, forms), `pdfplumber` (text and tables),
`reportlab` (new documents), installed in the sandbox. For a missing one, try
`python3 -m pip install`; with no network, say which library is missing.

## Read

```python
import pdfplumber
with pdfplumber.open("in.pdf") as pdf:
    for page in pdf.pages:
        print(page.extract_text()); print(page.extract_tables())
```

No text comes back from a scanned page: say so, and OCR it with
`pytesseract` only if Tesseract exists.

## Merge, split, rotate

```python
from pypdf import PdfReader, PdfWriter
out = PdfWriter()
for name in ("a.pdf", "b.pdf"):
    for page in PdfReader(name).pages: out.add_page(page)
out.write("merged.pdf")
```

Split by adding chosen pages to a new writer; rotate with `page.rotate(90)`.

## Fill a form

`reader.get_fields()` lists the fields; copy the reader into a writer with
`writer.append(reader)` and call `writer.update_page_form_field_values(
writer.pages[0], {"name": "value"})`, then write it.

## Create

Use `reportlab.platypus` (`SimpleDocTemplate`, `Paragraph`, `Table`, `Spacer`)
and `build([...])`. For a styled report prefer HTML or Typst when installed.

## Check

Reopen the output, count pages and extract the text of the first page.

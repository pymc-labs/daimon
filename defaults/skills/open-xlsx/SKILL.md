---
name: open-xlsx
description: Create, read and edit Excel (.xlsx) spreadsheets with openpyxl and pandas. Use for any .xlsx or spreadsheet file request: a workbook to produce, or an existing one to read, fix or extend.
---

# Excel workbooks

Use `openpyxl` for structure, formulas and formatting and `pandas` for bulk data
(`pd.read_excel`, `df.to_excel`). If openpyxl is missing, run
`python3 -m pip install openpyxl`.

## Read

`load_workbook("in.xlsx", data_only=True)` gives the last values Excel computed;
without `data_only` you get the formulas. A file written by a script has no
cached values, so `data_only=True` reads `None` until it is opened in Excel.

## Create and edit

```python
from openpyxl import load_workbook, Workbook
wb = Workbook(); ws = wb.active
ws.append(["Item", "Qty", "Price", "Total"])
ws["D2"] = "=B2*C2"            # keep the logic live: write formulas, not results
ws.column_dimensions["A"].width = 24
wb.save("out.xlsx")
```

Edit an existing book with `load_workbook("in.xlsx")` (not `data_only`, or the
formulas are lost on save), then save under a new name. Use number formats
(`cell.number_format = "#,##0.00"`), freeze headers (`ws.freeze_panes = "A2"`),
and `openpyxl.chart` for charts.

## Check

Reload the saved file and read back the cells you wrote. Formulas are not
evaluated by openpyxl: when you need their values, compute them in pandas too
or convert with `soffice --headless --convert-to xlsx` (LibreOffice) to
recalculate, if it exists.

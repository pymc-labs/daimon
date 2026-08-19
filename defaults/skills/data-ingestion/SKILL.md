---
name: data-ingestion
description: Load whatever the request points at — one CSV or Excel export, twelve related tables, a folder of 500 emails, a 4 GB log you never load, a warehouse table you query in place — and record every source in run/manifest.json with the grain one row represents, the axis it is ordered by, its row count, columns and dtypes. Read as text and coerce on purpose — pd.read_csv inference turns order_id 00123 into the integer 123, and a paged pull that stops early looks identical to a complete one. Use when a user attaches a file or a folder, points you at a database, warehouse or API, or asks for analysis of data you have not loaded yet.
---

# Data ingestion

A load that "worked" is the most expensive failure in an analysis. Nothing
raises, a frame comes back, and four stages later a number is wrong because
`order_id` lost its leading zeros, a `Total` row became a data row, or the API
returned page one of forty. You decide the shape everything downstream assumes;
`run/manifest.json` is where you say so out loud.

**Applies to** whatever the request points at: one flat export, twelve related
tables, a folder of 500 emails, a 4 GB log you never load, a warehouse table you
query in place. **One entry per source** — every table, directory of parts and
warehouse query gets its own key in `run/manifest.json`, with a recorded `query`
and a verified count where the bytes are not yours to keep. **Enter here** when
data is not loaded and recorded yet; **stop here** when "what did I just get"
was the question; already-typed data skips the text read, not the entry.

## Read as text, then coerce on purpose

**Type inference is where downstream bugs are born.** `pd.read_csv` with no
`dtype=` reads `00123` as the integer `123`, deleting the zeros that make it an
identifier, and turns a column holding one `N/A` into `float64`. Read as string,
coerce only what you compute on, and count the casualties:

```python
import pandas as pd

text = pd.read_csv("run/raw/orders.csv", dtype="str",
                   keep_default_na=False, na_values=[""])
df = text.copy()
df["qty"] = pd.to_numeric(text["qty"], errors="coerce").astype("Int64")
df["created_at"] = pd.to_datetime(text["created_at"], format="ISO8601",
                                  errors="coerce", utc=True)
for col in ("qty", "created_at"):
    lost = text[col].notna() & df[col].isna()
    if lost.any():
        print(col, int(lost.sum()), "unparseable:",
              text.loc[lost, col].head(3).tolist())
```

`keep_default_na=False` keeps `NA`, `None` and `null` as literal text, not
someone else's idea of missing; `errors="coerce"` makes a bad value `NaT`/`<NA>`
and the loop stops it being silent. An ID you can do arithmetic on is a bug.

## Size and sniff the source before you load it

```bash
ls -l run/raw/orders.csv && head -c 400 run/raw/orders.csv   # .gz: zcat f.gz | head -c 400
```

That shows the real delimiter and the header depth; `ls -R` and `unzip -l` do
the same for a folder or an archive, and `file-handling` owns never reading a
whole data file to find out. Past a few GB nothing loads, and decompressing to
make it load fills the disk: leave the `.gz` alone, count from it with
`duckdb.sql("SELECT count(*) FROM 'log.csv.gz'").df()`, and fold a chunked
`pd.read_csv` in fixed memory.

## Every source has one trap, and this is it

| Source | How to read it | The trap |
|---|---|---|
| CSV / TSV, or a table pasted into the message | `pd.read_csv(..., dtype="str")`; a spreadsheet paste is tab-separated, so `sep="\t"` | European exports: `sep=";"`, `decimal=","`, `thousands="."` — the default read gives one column, or numbers as text; a survey's second header row of question wording, or a pasted `Total` row, arrives as data — peek first, as with Excel |
| Excel `.xlsx` | `pd.ExcelFile`, then `pd.read_excel(header=n)` | it is a formatted report, not a table — title rows, spacer rows, a `Total` row that becomes data |
| JSON / JSONL | one document is `json.load(f)` then `pd.json_normalize(doc["features"], sep="__")`; JSONL that fits in memory is `json.loads` per line into `pd.json_normalize(recs, sep="__")`, and past that project only the columns you need with `duckdb.read_json(path, columns={...})` — a `.gz` reads in place | `pd.read_json(lines=True)` "succeeds" and hands you a column of dicts; nested lists survive normalization as Python lists, so hold a geometry or a tag list out of the frame; a list comprehension of `json.loads` over a 4 GB log OOMs before it raises, and an auto-inferred schema silently drops keys absent from its sample |
| SQL file: `.sqlite`, or a `.sql` dump | `sqlite3` or duckdb, then query it | `SELECT *` on a view, or on a table with soft-deleted rows still in it; a dump is DDL + INSERTs in a vendor dialect, and regexing INSERT lines drops any row with an escaped quote |
| Warehouse or live database | query in place: `pd.read_sql` on a bounded SELECT, `count(*)` for the row count; only duckdb and pyarrow ship here, so `pip install sqlalchemy` plus the vendor driver (`psycopg2-binary`, `snowflake-sqlalchemy`, `sqlalchemy-bigquery` — a bare `postgresql://` DSN resolves to psycopg2, not psycopg 3) and take the DSN from an agent env credential, never from chat — `workspace-setup` owns adding one | extracting the table to get a frame — you cannot, and the manifest wants the `query` and the DSN with its credentials stripped, not a `sha256` |
| Paged API | the loop below | a short final page and an early stop look exactly like completion |
| Many parts: a folder, a zip, 12 sheets in one workbook, a 12-table export | same-shaped parts stack into one entry — `pd.read_excel(xl, sheet_name=None)`, then `pd.concat(parts, names=["plate"]).reset_index(level="plate")`, because the part's name is data; compare the parts' columns and dtypes before you stack, and differently-shaped tables get one entry each with the join left to `data-cleaning` | the part name discarded, so no row knows which sheet it came from; part 007 read with different dtypes than its siblings; or a join done in whatever cell needed it and logged nowhere |
| A collection: 500 emails, images, JSON documents | leave the items where they are and build an index frame — one row per item, carrying `path`, an item id and the attributes you pulled out; a collection can be one document rather than many files, and then a GeoJSON feature's own id stands in for the `path` | forcing the corpus itself into a frame. Decoding the domain — image internals, audio, protein sequences — is the analysis's job, not ingestion's |

An Excel sheet is a report, not a table — peek before you choose `header=`:

```python
import pandas as pd

xl = pd.ExcelFile("run/raw/report.xlsx")   # xl.sheet_names lists every tab
print(pd.read_excel(xl, sheet_name="Q3 Summary", header=None, nrows=8).to_string())
df = pd.read_excel(xl, sheet_name="Q3 Summary", header=3, dtype="str")
df = df[df["Region"].notna() & (df["Region"] != "Total")]
```

## A paged pull is incomplete until you have proved it complete

**The worst outcome here is a partial pull that looks whole** — half the rows
analyze cleanly and every conclusion is wrong. Assert the count you got against
the source's own total, and never read a rate limit as the end of the data:

```python
import os, time, requests

url = "https://api.example.com/v1/orders?page=1&per_page=100"
headers = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
rows, body = [], None
while url:
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 429:
        time.sleep(int(r.headers.get("Retry-After", "5")))
        continue
    r.raise_for_status()
    body = r.json()
    rows.extend(body["data"])
    url = body.get("next_page_url")
assert len(rows) == body["meta"]["total"], f"partial pull: {len(rows)} rows"
```

If no total is reported, say completeness is unverified. A 404 stops the run,
and a missing DSN or token is requested through `workspace-setup` rather than
pasted into chat — never invent a plausible frame.

## What you write: `run/raw/` and `run/manifest.json`

A chat attachment is a signed URL: `curl -sS "<url>" -o run/raw/<name>`, so
`run/raw/` holds the bytes as retrieved — there is none for a warehouse table
you query in place. Later stages write beside it; none edit what you wrote.

```python
import datetime, hashlib, json, pathlib
src = pathlib.Path("run/raw/orders.csv")
pathlib.Path("run/manifest.json").write_text(json.dumps({"orders": {
    "source": "https://api.example.com/v1/orders",
    "retrieved_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "grain": "one row per order line, ordered by created_at",
    "rows": len(df),           # or a duckdb count(*), when nothing was loaded
    "cols": list(df.columns),  # just the projection, when that is all you read
    "dtypes_as_read": {c: str(t) for c, t in df.dtypes.items()},
    "sha256": hashlib.file_digest(src.open("rb"), "sha256").hexdigest(),
}}, indent=2))
```

Required per entry: `source`, `retrieved_at`, `grain`, `rows`, `cols`,
`dtypes_as_read`, and either a `sha256` — for a directory of items, the item
count and the decode failures you verified instead — or, bytes not yours, the
`query` plus a count checked against the source's own total. `grain` is the
sentence "one row of this represents ___, ordered by ___": per (store, week), by
week. Ask when the source will not say. Name the ordering column too: validation
and EDA bucket along the axis you name, and a composite grain has more than one.

Hand the manifest to `data-validation`; you asserted only that the data is
loaded and counted, so if the answer ships from here, name the stages nobody
ran. A 40-row paste needs no `run/`; the manifest is one sentence in your reply.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `UnicodeDecodeError`, or names arrive as `Ã©` / `ï»¿id` | the export is cp1252, or has a UTF-8 BOM | `encoding="cp1252"` or `encoding="utf-8-sig"`, chosen from the peek |
| `df["Region"]` raises `KeyError` though the header looks right | trailing whitespace in the header cells | `df.columns = df.columns.str.strip()` before anything else |
| Dates all land in the first 12 days of each month | `d/m` vs `m/d` resolved per value | pass an explicit `format=`, and count the `NaT`s the coerce produced |
| A date column holds `45231` | Excel serial dates | `pd.to_datetime(s.astype("Int64"), unit="D", origin="1899-12-30")` |
| `df.dtypes == object` selects no columns | pandas 3 reads text as `str` dtype, not `object` | `pd.api.types.is_string_dtype(s)`, or `df.select_dtypes("str")` |
| Two pulls minutes apart return different row counts | a live source paginating over a moving table | sort by a stable key server-side; `retrieved_at` in the manifest is what dates the answer |

---
name: data-cleaning
description: Mutate data without destroying the evidence — dedup, dtype coercion, missing-value decisions, outlier flagging, category and unit normalisation, joins, pivots and aggregations to a new grain, and columns the source does not contain — appending a row to run/changelog.jsonl for every operation, whether what you write is one parquet file, one part per table, parquet parts of a log too large to load, a saved warehouse query, or cleaned files plus their index frame. The source is never edited in place, and under pandas 3 copy-on-write a chained assignment like df[mask]["col"] = 0 silently changes nothing. Use when a validation check failed, when the grain has duplicates, wrong dtypes or unparseable dates, when a source is too large or raw to query repeatedly and must be materialised once as typed parquet parts, when a collection needs an index frame derived, when a metric or label no column holds must be defined, or when asked to clean, dedup, impute, join, reshape or fix a dataset.
---

# Data cleaning

Cleaned data with no record of what changed is worse than the dirty data it came
from: nobody can separate a real 40% collapse in orders from a dedup on the wrong
key. This is the only stage that mutates data, and it runs only if something does.

**Applies to** one flat export, twelve related tables, a 4 GB log you never load,
a warehouse table queried in place, 500 emails in a folder. **Clean in the medium
the data lives in** — a parquet file, filtered parts, a saved query, or cleaned
items plus their index frame, and deriving that frame or a column no source holds
is a mutation like any other. **Enter here** when a cell, a row count or the grain
changes, **stop here** when the cleaned data is the deliverable.

## Never edit the source, and never mutate without a log row

**The source is the one thing you cannot recreate.** Re-read it as text and coerce
as `data-ingestion` did, write beside `run/raw/` and never into it, and clean data
queried in place with a saved `SELECT`, never an `UPDATE`.

**Every operation that changes a cell, a row count or the grain appends one row.**
The rationalization is always "this one is obvious", and a whitespace strip that
collapsed two distinct SKUs leaves no trace at all. Scale the trail to the data,
never the checks: one small source states each mutation inline in its reply.

```python
import json, os, pandas as pd
df = pd.read_csv("run/raw/orders.csv", dtype="str", keep_default_na=False, na_values=[""])
df["qty"] = pd.to_numeric(df["qty"], errors="coerce").astype("Int64")

def log_change(op, column, rows_affected, rule, before, after):
    os.makedirs("run", exist_ok=True)
    with open("run/changelog.jsonl", "a") as fh:
        fh.write(json.dumps({"op": op, "column": column, "rule": rule,
            "rows_affected": int(rows_affected), "before": int(before), "after": int(after)}) + "\n")
```

## Write every edit through `.loc`

Under pandas 3 copy-on-write, chained assignment only warns, leaves the frame
unchanged, and makes the log claim an edit that never happened. Same for
`inplace=True` on a slice.

```python
# ❌ SILENT NO-OP — warns, and qty is still -1 afterwards
df[df["qty"] < 0]["qty"] = 0
# ✅ one step, and the mask gives you the count to log
mask = df["qty"] < 0
df.loc[mask, "qty"] = 0
log_change("clip_to_zero", "qty", mask.sum(), "negative qty is impossible", len(df), len(df))
```

## Every treatment has a default, and a case where the default is wrong

| Problem | Default treatment | When not to |
|---|---|---|
| Duplicate on the grain | sort by a tiebreaker, keep one, log the rule | the copies disagree on a column you care about — that is a source bug |
| Wrong dtype from read | `astype`, or `pd.to_datetime(errors="coerce")` | over ~1% fails to parse — a read bug, see the `data-ingestion` skill |
| Outliers | flag in a new boolean column, from quantiles taken per group when one frame pools several populations | the value is impossible, not merely extreme — then it is a coercion |
| Free-text categories | map through an explicit dict, assert nothing is unmapped | the long tail is the finding, not noise |
| Mixed units, or a scale pointing the other way | convert to one unit and one direction, rename the column to carry it (`q7_reversed`) | the unit is not recoverable per row — stop and ask |

## Normalise and coerce before you deduplicate

Both change what counts as equal. Normalise through an explicit dict, asserting
`set(key.dropna()) - set(MAPPING)` is empty first — a `.fillna(original)` fallback
is how "Canada " and "CA" reach the report as two categories.

```python
parsed = pd.to_datetime(df["ordered_at"], errors="coerce", format="ISO8601")
lost = df["ordered_at"].notna() & parsed.isna()       # errors="coerce" nulls silently
print(df.loc[lost, "ordered_at"].head().tolist())     # look before you accept it
df["ordered_at"] = parsed
log_change("to_datetime", "ordered_at", lost.sum(), "ISO8601, bad -> NaT", len(df), len(df))
```

Then deduplicate, where the grain admits it — a repeated sensor reading is data,
not a duplicate. Where it is one, a tiebreaker, then `df.drop_duplicates(`.

## Leave a gap rather than invent a value

| Option | Use when | The trap |
|---|---|---|
| Leave it — the default | almost always; pandas skips nulls in `mean`, `sum`, `groupby` | a later join or `groupby` drops those rows without saying so |
| Drop rows | the null makes the row unusable for this question | bare `dropna()` is `how="any"` over every column and halves a wide frame |
| Impute | you have a stated reason, you log the method, and you add a `<col>_imputed` boolean so EDA can split on it | "fill missing revenue with the column mean so the totals work" is the banned move — a mean into a column whose distribution you then report shrinks the variance and manufactures precision |

## Flag outliers; a dropped outlier is a deleted finding

An extreme value is only wrong if it is impossible: a 90,000 order is a fact until
someone proves it a typo, and the biggest customer is what a blind 3-sigma filter
deletes first. Flag it — `(df[c] < q1 - 3*(q3-q1)) | (df[c] > q3 + 3*(q3-q1))`,
from `df[c].quantile([0.25, 0.75])` or `df.groupby(circuit)[c]` when one frame
pools several populations, into a `<col>_outlier` boolean, `before` and `after`
equal. Impossible values are the other case: coerce or drop those.

## What you write, and who reads it

Only the data changes shape; the log never does. Downstream resolves the data
positionally — `run/clean.parquet`, else `run/clean/`, else `run/clean.sql`.

| Data | Cleaned artifact |
|---|---|
| One frame | `run/clean.parquet` |
| Related tables, extracted | one parquet per table under `run/clean/`, plus `joined.parquet` when a join is the analysis grain |
| Too big to load | `run/clean/part-*.parquet`, from `duckdb.sql("COPY (SELECT …) TO 'run/clean' (FORMAT parquet, PER_THREAD_OUTPUT true, FILENAME_PATTERN 'part-{i}')")` — bare `TO 'run/clean'` writes one CSV *file* called `clean` |
| Queried in place | `run/clean.sql`, the SELECT that produces the grain — including the join when several tables are queried together — plus a bounded `run/clean_sample.parquet` |
| Files, not rows | cleaned items under `run/clean/items/`, index frame at `run/clean.parquet` — deriving that frame is the mutation, so this stage runs for a collection even when no cell changes |

`run/changelog.jsonl` holds one object per transformation in applied order —
`{"op", "column", "rows_affected", "rule", "before", "after"}` — ending in a
terminal `{"op": "write_clean", "path": …, "format": …}` row that tells the next
stage where the data is. `rule` says *why*, and the new grain when one changes:
"keep earliest `created_at` per `order_id`", not "removed duplicates". Anything
that changes the grain lands here alone — a `df.merge(`, a `df.pivot_table(` whose
`aggfunc` and `fill_value` you chose, an aggregation of events to sessions —
proved not to have fanned out by `validate="1:m"`, or in SQL by an unchanged
`count(*)` and `count(distinct …)` either side; then re-validate on the new grain.
A metric no column holds — churn, retention, a per-item label — is a definition
you take from the user and log as the `rule`.

Re-run `data-validation` on what you wrote; one treatment routinely breaks another
check. Then hand it to `exploratory-data-analysis` or to modeling, the log going
with it for `eda-storytelling`. You may be the last stage: name any stage you
skipped that could have changed the answer — and on a `pass` with nothing to
treat, say so and pass the source on with the manifest's dtypes.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| An edit "ran" but the values are unchanged | chained assignment — pandas 3 only warns with `ChainedAssignmentError` | one step: `df.loc[mask, col] = value` |
| `TypeError: Invalid value '0' for dtype 'str'` | assigning a number into a column ingestion read as text | coerce the column first, or assign the string |
| Row count fell further than the log accounts for | bare `dropna()` (`how="any"` over all columns), or a merge that dropped non-matches | subset the columns; log `before`/`after` around every mutation |
| Row count *rose* after a later join, or a reshape averaged duplicate rows and left absent cells `NaN` | the dedup key was not the grain, so the join went many-to-many; `df.pivot_table(` defaults to `aggfunc="mean"` and fills nothing | pass `validate="1:m"` to `df.merge(`, and `aggfunc="sum"` with `fill_value=0` deliberately; re-check the grain |
| `run/changelog.jsonl` holds operations from an earlier attempt | `log_change` appends, and the file survives a re-run | delete `run/changelog.jsonl` when you restart the run, not at the end |

---
name: data-validation
description: Decide whether data is fit to analyze before anyone analyzes it. A phase-1 pass runs in seconds — dtypes, the grain one row claims to represent, referential gaps between tables, plausibility ranges, coverage — and emits run/validation.json with a pass/warn/fail verdict; a fail is a stop, not a to-do item. Works the same on a flat export, twelve related tables, a keyless sensor stream, an index frame over 500 documents, or a warehouse table you check with SQL and never load. Phase 2 hunts the structural break — a definition, unit, currency or timezone that changed partway along whatever the data is ordered by. Use when data arrives from ingestion, before any chart, join or model, or when a number looks wrong and you cannot say why.
---

# Data validation

Data that loads without error is not data that is admissible. The duplicate order
line that doubles revenue, the `channel` whose vocabulary changed in May, the two
timezones in one column — none raise; the only signal is a plausible-looking
answer. You check each source `run/manifest.json` names and emit one verdict.

**Applies to** one flat export, twelve related tables, a 4 GB log or a warehouse
table you never load, 500 emails as an index frame. **No key, no duplicate check**
— a stream or an event log has an index, not a key, and repeated readings are
data; check ordering and gaps, per-group counts against the replicates you expect
for nested data, and join keys before a join. **Enter here** before any number
ships; **stop here** on `fail`, or when "is this usable" was the question.

## Phase 1 runs first, and a fail verdict stops the analysis

**Nothing gets reported before phase 1 — no chart, no number, no statement about
what the data shows.** Looking at the data to decide what to check is fine;
publishing is not. Take the expected columns, grain and ranges from the user, and
write masks they can read — a check derived from the data always verdicts `pass`.

```python
import json
import pandas as pd

df = pd.read_csv("run/raw/orders.csv", dtype="str", keep_default_na=False, na_values=[""])
df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
key = ["order_id"]  # several for a composite grain; [] for an index, then whole-row dupes
locator = df[key].astype(str).agg("|".join, axis=1) if key else df.index.to_series().astype(str)
checks = []

def record(check, severity, failing):
    checks.append({"check": check, "severity": severity, "n_failing": int(failing.sum()),
                   "example_ids": locator[failing].head(5).tolist()})

record("grain is one row per order_id", "fail", df.duplicated(subset=key or None, keep=False))
record("amount is positive", "fail", df["amount"] <= 0)
record("created_at parses", "fail", pd.to_datetime(df["created_at"], errors="coerce").isna())
record("email is present", "warn", df["email"].isna())
failed = [c for c in checks if c["n_failing"]]
verdict = "fail" if any(c["severity"] == "fail" for c in failed) else "warn" if failed else "pass"
json.dump({"checks": checks, "verdict": verdict}, open("run/validation.json", "w"), indent=2)
print(verdict, [(c["check"], c["n_failing"]) for c in failed])
```

**On `fail`, your entire reply is the failure**: the check, `n_failing`, the
example ids, and the treatments available. Dedup changes the revenue number you
are about to report, so the user picks it. On `warn`, say it inline.

**That includes the conditional.** "If you confirm dedup, the total would be
$260" is the number shipping with a hedge attached — the reader keeps the figure
and forgets the condition, and you have pre-committed them to a treatment they
never chose. Name the treatments and what each would change about the shape of
the answer, not the value it would produce. Compute it on the next turn, after
they pick.

**When you cannot load it, check it in place** — aggregates where the data lives,
duckdb over a file or the warehouse's own SQL, one `count(*) FILTER (WHERE …)` per
check plus a `LIMIT`ed sample of ids, into the same `run/validation.json`.

## Check admissibility, not whatever the source happens to contain

| Check | How to detect | What it blocks |
|---|---|---|
| Schema and dtype | `set(df.columns)` against the expected set; `df.dtypes` against `dtypes_as_read` in `run/manifest.json` | a numeric column read as text — every aggregate silently wrong |
| Grain — what one row is | keyed: `df.duplicated(subset=key, keep=False)`, composite for a panel; sequence-indexed: `idx.is_monotonic_increasing`, `idx.duplicated()`, and `idx.diff().value_counts()` for gaps and sampling-rate changes; grouped: `df.groupby("plate").size()` against the replicates you expect; a collection: `path` is unique by construction, so hash the contents and look for thread copies and auto-replies | every sum, count and join; duplicates multiply, and a gap read as a zero flattens a trend |
| Referential integrity | `(~df["customer_id"].isin(dim["customer_id"])).sum()`, on every key a join will use | inner joins that drop rows without saying so — `data-cleaning` performs the join, you clear its keys first |
| Plausibility range | one mask per column — negative amounts, ages over 120, future timestamps; and `df.nunique() == 1`, a constant column that correlates with nothing and returns `NaN` | means, and every ratio built on them |
| Unit, currency, timezone, CRS | `df.groupby("country", observed=True)["amount"].median()`; `pd.to_datetime(col, errors="coerce", utc=True)`; coordinates inside ±180 and ±90, and not swapped — a swap survives a range check wherever both are under 90 | totals across mixed units; any daily aggregate; every distance and every cluster |
| Coverage | `bucket.value_counts().sort_index()` against the range requested | a trend drawn over buckets absent from the source; and a span straddling the first or last bucket is truncated, not finished — a session that began before the log starts looks like it abandoned at step one |

## Phase 2 hunts the structural break, which phase 1 cannot see

A structural break is a definition, unit or collection method changing partway
along whatever the data is ordered by — month, trial number, wavelength. Every
phase-1 check passes and the column is still two columns glued end to end: an
attribution window that changed in May, a sensor whose rate halved.

```python
import pandas as pd

df = pd.read_csv("run/raw/orders.csv", dtype="str", keep_default_na=False, na_values=[""])
# bucket along the index the manifest names — months, trial blocks, firmware
# versions — fine enough to span ten of them; a GROUP BY where it lives if huge
bucket = pd.to_datetime(df["created_at"], errors="coerce").dt.to_period("M")
levels = df.groupby(bucket, observed=True)["channel"].agg(lambda s: set(s.dropna()))
for prev, cur in zip(levels.index[:-1], levels.index[1:]):
    if levels[cur] ^ levels[prev]:
        print(cur, "vocabulary changed:", sorted(levels[cur] ^ levels[prev]))
panel = df.groupby(bucket, observed=True).agg(
    n=("order_id", "size"), null_rate=("email", lambda s: s.isna().mean()),
    mean_amount=("amount", lambda s: pd.to_numeric(s, errors="coerce").mean()))
print(panel.round(3).to_string())
```

A step in `n`, a null rate or a mean is the signature, and so is one level unlike
the rest; a drift is not. `TV` where `tv` vanished is a rename.

## Detect and report; never repair

**No `fillna`, `astype`, `df.drop_duplicates`, `clip` or join here.** A treatment
applied here never reaches `run/changelog.jsonl`; `data-cleaning` owns every
mutation, and a read bug goes back to `data-ingestion`, not forward.

## Write run/validation.json, then hand off

Keep passing checks in the file; a check nobody ran must not look like a pass. A
phase-2 break is a check row too, naming its bucket in `check`. **Scale the
trail to the data, never the checks** — 40 rows pasted in the message get the
same grain, dtype and coverage checks and the same look for a definition that
changed late, stated as a clause in your reply instead of written under `run/`.

You may be the first stage here and you may be the last. On `fail`, or a `warn`
the user accepted, name the treatment, hand to `data-cleaning`, then re-run this
stage on what it wrote — a cleaned frame not re-validated is unproven. On `pass`
with nothing to treat, cleaning is ceremony: say so, hand the source on with the
dtypes the manifest recorded. If the answer ships from here, end the reply with
the literal `Stages: … · skipped: …` line `eda-storytelling` defines.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Totals are roughly double the finance number | duplicate rows on a composite grain you assumed was a primary key | the key is `(order_id, line_no)`, not `order_id` — re-check `df.duplicated(subset=key)` |
| A trend inflects exactly at a bucket boundary | structural break — definition or collection changed | phase 2; two series, or stop and ask |
| Every check passes but a mean is absurd | mixed units or currencies in one column | median by group; a 100× gap is a unit, not a customer |
| Daily counts show two humps a day apart | naive timestamps in mixed timezones | `pd.to_datetime(col, utc=True)`, then confirm what the source meant |
| The user says the data is already clean | "clean" says where it came from, not what was checked; a BI export is aggregated and re-stated | run phase 1 anyway, and ask what the tool already aggregated or attributed |

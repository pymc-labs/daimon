---
name: exploratory-data-analysis
description: Profile and interrogate data before anyone makes a claim about it — a cleaned frame, a folder of parquet parts, a warehouse table you query in place, or an index frame over a corpus of documents. Summary statistics do not identify a distribution — Anscombe's quartet shares a mean, variance and correlation across four unrelated shapes — so nothing is reported that has not been plotted. Covers dtype and cardinality profiling, missingness structure, Spearman against Pearson, a correlation matrix read as blocks instead of skimmed for its biggest cells, subgroup checks and whether one named group, batch or run is an outlier against the rest, variation along whatever index orders the data, writing one row per finding to run/findings.jsonl and handing it on to modeling. Use when asked to explore or profile a dataset, before a model is fit, when one group, device, batch or period looks wrong and you need to say how it differs, or when a pile of numbers has to become defensible findings.
---

# Exploratory data analysis

Anscombe's quartet is four datasets sharing a mean, variance, correlation and
regression line, and four completely different shapes. A statistic you have not
plotted is not evidence. You look at every column you mean to talk about, and
append one row per finding to `run/findings.jsonl`.

**Applies to** whatever earlier stages left you: one cleaned frame, parquet
parts too big to load, a warehouse table you query in place, an index frame over
500 emails. **Your index may not be a calendar** — trial number, wavelength and
sequence position order data too, so bucket along the manifest's `grain`.
**Enter here** once the data is admissible, **stop here** when a description
answers it — *why* and *what drives* want a fit, so hand that on to modeling.

## Look at a column before you summarise it

**Never report a mean, a correlation or a slope you have not seen plotted.** A
second mode, a floor at zero, a ceiling at 100, a spike at a `-999` sentinel and
one 10,000× outlier all leave the mean printable and change what it means.
Resolve your input: `run/clean.parquet`, else `run/clean/`, else `run/clean.sql`
— aggregate with it, plot the `run/clean_sample.parquet` beside it — else the
manifest's source with the dtypes it recorded, else the paste itself.

```python
import pandas as pd
df = pd.read_parquet("run/clean.parquet")  # parts or in place: duckdb aggregates, then a sample
print(len(df), "rows")
print(pd.DataFrame({"dtype": df.dtypes.astype(str), "pct_missing": df.isna().mean().round(3),
                    "n_unique": df.nunique(dropna=True)}))
print(df.select_dtypes("number").quantile([0, .01, .25, .5, .75, .99, 1]).T)
```

Percentiles .01 and .99 beside min and max are where a sentinel or a misplaced
decimal shows; `.describe()` hides both. Styling is `pymc-artifact-style`'s.

## Match the method to the question

| Question | Compute | Chart form |
|---|---|---|
| What is in this column? | quantiles at 0/.01/.25/.5/.75/.99/1, `nunique`, `skew()` | histogram, 50+ bins; ECDF when the tail is long |
| What is in this collection? | the index frame's own profile — `n_chars` per document, pixel dims, coordinate bounds, node degree — then quantiles as above | histogram of item size; iterate every item in code, read only a sample into context (`file-handling`); plot coordinates in projected metres, since a degree of longitude shrinks with latitude and there is no basemap here |
| Do two numerics move together? | `corr("pearson")` and `corr("spearman")` | scatter, with binned medians over it |
| Do groups differ? | `groupby().agg(n=…, median=…)`, then the same aggregate within each level of anything that could have produced the difference | small multiples or box plots, never a bare bar of means |
| Does it vary along the index? | count *and* metric per bucket of whatever the `grain` orders — month, trial, wavelength bin, store | line of the row count first, the metric second; drop or label a partial final bucket |
| Which rows — or which named group — are extreme? | distance from the median in MADs; for a group, summarise per group first, then MADs across the group summaries, and say what `n` groups supports | scatter with the flagged points marked; group summaries as a strip plot |

## Missingness is a variable, not a gap

**Test whether missingness depends on something before you treat it as noise.**
Missing-at-random and missing-because-of-something print the same null rate and
mean opposite things.

```python
missing = df["email"].isna().rename("email_missing")
bucket = df["created_at"].dt.to_period("M")   # or trial // 100, or a wavelength bin
print(missing.groupby(df["region"]).mean().round(3))  # by segment
print(missing.groupby(bucket).mean().round(3))        # along the index
print(df.groupby(missing)["amount"].median())         # vs the outcome
```

Flat across segments and buckets is collection noise. A jump at one boundary is
a schema change, and that window may not be comparable. A rate that tracks the
outcome biases any analysis that drops those rows — report it, do not treat it.

## Check every relationship against rank and against subgroup

**Compute Spearman alongside Pearson and read the pairs where they disagree.** A
large gap means the relationship is curved, or that a few points carry the fit.

```python
num = df.select_dtypes("number").drop(columns=["order_id"])
pairs = pd.DataFrame({"pearson": num.corr("pearson").stack(),
                      "spearman": num.corr("spearman").stack()})
pairs = pairs[pairs.index.get_level_values(0) < pairs.index.get_level_values(1)]
pairs["gap"] = (pairs["spearman"] - pairs["pearson"]).abs()
print(pairs.sort_values("gap", ascending=False).round(3).head(10))
print(df.groupby("region")[["amount", "tenure_days"]].corr("spearman")
        .xs("amount", level=1)["tenure_days"].round(3))   # a sign no group repeats is noise
```

## What you must not compute

- **No correlation matrix skimmed for its biggest cells** — 40 columns give 780
  pairs and the largest are what noise produces. Read it as structure: cluster
  or factor it, show the reordered heatmap, say how many blocks you kept.
- **No p-value or significance claim from a comparison you chose after seeing
  the data.** Report effect size, direction and `n`. A test, a fit or a causal
  claim is modeling output — modeling is a real next stage when `n` can support
  one; see the handoff below.
- **No edits to the frame.** Flag an outlier; `data-cleaning` treats and logs it.
- **No pattern reported before you ask whether collection made it** — a cliff at
  a round number, a mode at the form default, a gap on weekends.

## What you write: run/findings.jsonl

One row per finding, appended as you go; `how` is the expression that re-derives
the number — pandas, SQL or a shell one-liner. When `n` cannot carry a claim,
the finding is the plot, the direction and the size, and the `caveat` says so.

```json
{"id": "missing-email-rate", "claim": "40% of rows have no email", "value": 0.404, "n": 12043, "how": "df['email'].isna().mean()", "caveat": "concentrated in the east region"}
```

Charts stay under `run/`; a notebook to poke at is `marimo_notebooks`'s. You may
be the whole answer or one step in it: run this stage, skip what nobody asked
for, and name a skipped stage that could change the answer.

Hand `run/findings.jsonl` to `eda-storytelling`, which picks what the reader
sees, a posterior included — and to modeling when a fit was asked: a
`pymc-marketing-*` skill (mmm, clv, priors) if one is attached, otherwise the
simplest PyMC model that answers the question at this `n`. Name its likelihood
and priors, say what a real MMM or CLV has that yours lacks (adstock,
saturation, a hierarchy), and do not ship it under that name. Record what a
profile lacks: the
grain one row represents, the index column and its regularity, which columns
group rows, their level counts, what nests in what, each item or covariate's
unit, missing rate and range, the outcome's distribution and any floor or
ceiling, and every Pearson/Spearman gap that says a relationship is curved.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| A "strong correlation" that no segment reproduces | pooled Pearson driven by group sizes or a few points | Spearman plus the per-group check above; report the subgroup result |
| Mean and median disagree wildly, the write-up quotes the mean | heavy tail or an un-flagged outlier | Quote the median with an IQR, flag the tail as its own finding |
| The reply is 400 lines of `.describe()` and a pairplot | volume substituted for judgement | Cut to findings that change a decision; the rest stays in `run/` |
| A claim has no `findings.jsonl` row | the number was computed in passing, never recorded | Record it or drop it — `eda-storytelling` rejects unsourced numbers |

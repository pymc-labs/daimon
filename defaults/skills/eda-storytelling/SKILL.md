---
name: eda-storytelling
description: Turn a finished analysis into something a person reads and acts on — lead with the finding that settles the question or moves the decision, trace every number back to a run/findings.jsonl, run/changelog.jsonl or run/manifest.json record, and cut the rest. Works from whatever the analysis produced — one flat export, twelve joined tables, a warehouse table queried in place, an index frame over a corpus, a fitted model's posterior. Carries the data-validation verdict, the data-cleaning change log and any stage nobody ran into the writeup as caveats instead of burying them. Use when a single data question has to be answered in one chat reply, when writing up an analysis, when drafting a report, summary or readout of what the data or a fitted model showed, or when a pile of charts and tables needs an argument.
---

# EDA storytelling

The failure is the stats dump: nine charts, a `describe()` transcript, a
correlation matrix, no argument. It reads as thorough and is unusable — the
reader cannot tell which of forty numbers mattered, so acts on none. You consume
`exploratory-data-analysis`'s `run/findings.jsonl` and the `run/validation.json`
verdict, and emit one document: what is at stake, and the numbers that settle it.

**Applies to** whatever the analysis produced: one flat export, twelve joined
tables, a 4 GB log nobody loaded, a warehouse table queried in place, an index
frame over 500 emails. **A skipped stage is a caveat, not a silence** — the
record behind a number may be a manifest entry, a validation check, a change-log
row or a findings row, and a stage nobody ran is named, with why. **Enter here**
when there are numbers and a reader; **stop here** — you are the last stage.

## Every number traces to a record, or it does not ship

**A claim with no `run/findings.jsonl` or `run/changelog.jsonl` row behind it is
a fabricated number**, however sure you are you computed it earlier — recalled
values come back rounded, pre-cleaning, or carrying the wrong `n`. Cite by `id`
while drafting, strip the markers in the final pass, and check between:

```python
import json, re
from pathlib import Path
rows = [json.loads(line)
        for p in (Path("run/findings.jsonl"), Path("run/changelog.jsonl")) if p.exists()
        for line in p.read_text().splitlines() if line.strip()]
man = Path("run/manifest.json")            # a row count traces to a source entry
ids = ({r.get("id") or r["op"] for r in rows}
       | (set(json.loads(man.read_text())) if man.exists() else set()))
cited = set(re.findall(r"\[([a-z][a-z0-9_-]*)\](?!\()", Path("draft.md").read_text()))
assert not cited - ids, f"claims with no backing record: {sorted(cited - ids)}"
```

Recompute an unbacked number and append its row, or delete the sentence — there
is no third fix. Scale the trail to the data, never the checks: a paste answered
inline has no `run/` to cite, and every number in it is computed this turn.

## Lead with the finding that settles the question

**Name what is at stake in the first two sentences** — the decision the reader
is choosing between, or, in diagnostic or scientific work, the question they
asked — then the finding that moves it. Method and `n` follow in the same
paragraph, never as a preamble; order by how much a finding moves the answer,
not by pipeline stage. **Cutting is the skill:** of thirty findings two reach
the reader, the rest stay in `run/findings.jsonl`. When the data cannot settle
the question, that is the headline — "this cannot tell you whether the campaign
worked, because exposure was never recorded" is a deliverable, and at n=40 so is
"these 40 points cannot separate a trend from noise".

## Every chart answers a question you already asked in the prose

The sentence before states the question, the chart answers it, the one after
says what it means. **If you cannot write that question, cut the chart** — it
exists because it was easy to make. One claim per chart, never two.

## State the uncertainty once, beside the number, then stop

Carry the `n` and the `caveat` from the finding's row into the sentence making
the claim; a limitation covering the whole analysis is stated once, in the
caveats block. Hedging every sentence says nothing while looking careful.

- Round to what `n` supports: 41% of 11,731, not 41.0126%.
- Say **measured** or **estimated**; anything imputed or fitted is estimated,
  and a number resting on a definition you chose — a 30-minute session window,
  a funnel step order, a complaint taxonomy — names that definition beside it.
- Give an association its direction and size. Significance and cause are model
  output: cite the model that produced them, or do not write them in.

## Carry the verdict and the change log in as caveats, not footnotes

From `run/validation.json`: a waived check that could move a reported number is
a caveat, stated at that number; an unresolved FAIL is not a caveat — the claim
does not ship. Cite change-log rows as "312 duplicates dropped, earliest kept".

| Change-log row | Tell the reader | Why |
|---|---|---|
| imputation, outlier treatment, a derived label or category, drops hitting a reported segment | yes, at the claim | it can bias the number they act on |
| dedup removing more than 1% of rows | yes, once in the caveats | it moves every denominator |
| dtype coercion, whitespace strip, rename, the terminal write-clean row | no | it cannot change a reported number |

**A stage nobody ran is a caveat too, and it is a required line, not a mood.**
Every writeup ends with one literal line naming both sides:

```
Stages: ingestion, validation (pass) · skipped: cleaning (nothing to treat), EDA (not asked)
```

Write it even when nothing was skipped — then it reads `skipped: none`, and the
reader learns the chain ran rather than guessing. Interrogating a claim that the
data is already clean is not the same as validating it: if you did not write
`run/validation.json` this turn, validation is skipped, however clean the data
looked. Write up what ran; do not run the rest to tidy the line away.

## The same numbers, twice

```
❌ everything computed, nothing decided
11,731 rows x 14 cols. email 41.0% null | phone 12.3% null | source 0.0% null
Spearman: email_missing~signup_year 0.62, order_value~tenure 0.31, ...
[8 charts] The data shows interesting patterns. Further analysis is recommended.
```

```
✅ the same findings.jsonl rows, written for a reader
We cannot email 41% of the customer list, and the gap is not random.

Of 11,731 customers, 4,810 have no email, almost all from the 2023 partner
backfill: 89% of those rows lack one against 6% of normal signups. So a campaign
here reaches about 6,900 people, not 11,700, and it drops partner-sourced
customers, who spend more per order (median $84 against $61, n=11,731). Budget
for the smaller reach, or pull emails from the partner feed first — the excluded
segment is the higher-value one. All figures are measured, none imputed; the 312
dropped duplicate order_ids move every count by under 3%.
```

**Sentences that never ship:**

- "Correlation does not imply causation" *instead of* stating the correlation.
- Any conclusion the reader cannot check against a number you gave them.

## Pick the medium, then hand the rendering off

One finding answers in the chat reply; a client deliverable is a PDF report
styled by `pymc-artifact-style`; to poke at the data they want a notebook
(`marimo_notebooks`). Both land in `/mnt/session/outputs/`, per `file-handling`.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| User replies "so what?" | No question or decision named; findings ordered by pipeline stage | Open with what is at stake and the one finding that moves it |
| A number in the draft has no backing row | It came from a scrolled-away cell or from memory | Recompute and append the row with its `how`, or cut the sentence |
| A claim rests on a stage nobody ran | Validation or cleaning was skipped and the writeup does not say so | Name the skipped stage as a caveat at that claim, or run it |
| Every sentence hedges | Uncertainty restated per sentence instead of once per claim | One `caveat` at the claim; global limits go in the caveats block once |
| Reader acts on a number cleaning invented | Imputed values reported as measured | Label the layer estimated and cite the imputation change-log row |
| A conclusion asserts cause | An EDA correlation written up as a mechanism | Give direction and size; a causal claim needs a model, cited as one |

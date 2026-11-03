#!/usr/bin/env python3
"""Figures for report.md, from the pymc-marketing example data bundled in notebook/.

    python3 make_figures.py        # writes roi.png and fit.png next to this file

A quick regression, not an MMM: KPI on spend with a fixed carry-over (half the
effect each following week, 8 weeks), trend, yearly seasonality and the two
events; 90% intervals from 2,000 resamples of 8-week residual blocks.
Needs numpy, pandas, matplotlib. Prints the numbers report.md quotes.
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SKILL = HERE.parents[1]
for f in (SKILL / "fonts").glob("*.ttf"):
    fm.fontManager.addfont(str(f))
matplotlib.rc_file(SKILL / "mpl" / "matplotlibrc")
plt.rcParams.update({"font.size": 11, "legend.fontsize": 10, "figure.dpi": 100})

df = pd.read_csv(SKILL / "notebook" / "mmm_example.csv", parse_dates=["date_week"])


def carry(x, a=0.5, L=8):
    w = a ** np.arange(L)
    return np.convolve(x, w)[: len(x)] / w.sum()


t = df.t.values / df.t.max()
doy = df.dayofyear.values / 365.25 * 2 * np.pi
X = np.column_stack([np.ones(len(df)), t, np.sin(doy), np.cos(doy), np.sin(2 * doy), np.cos(2 * doy),
                     df.event_1, df.event_2, carry(df.x1.values), carry(df.x2.values)])
y = df.y.values
b = np.linalg.lstsq(X, y, rcond=None)[0]
res = y - X @ b
rng, n, draws = np.random.default_rng(7), len(y), []
for _ in range(2000):
    idx = np.concatenate([np.arange(s, s + 8) % n for s in rng.integers(0, n, n // 8 + 1)])[:n]
    draws.append(np.linalg.lstsq(X, X @ b + res[idx], rcond=None)[0])
draws = np.array(draws)

rows = []
for name, col, j in [("Channel A (x1)", "x1", 8), ("Channel B (x2)", "x2", 9)]:
    contrib = draws[:, j] * X[:, j].sum()
    roi = np.percentile(contrib / df[col].sum(), [5, 50, 95])
    share = np.percentile(contrib / y.sum(), [5, 50, 95])
    rows.append((name, roi))
    print(f"{name}: weeks with spend {(df[col] > 0).sum()}, KPI per unit {roi.round(0)}, share {share.round(3)}")
print(f"R2 {1 - res.var() / y.var():.2f}, weeks {n}")

fig, ax = plt.subplots(figsize=(8, 2.2))
for i, (name, (lo, m, hi)) in enumerate(reversed(rows)):
    ax.plot([lo, hi], [i, i], color="#0C1F40", lw=3, solid_capstyle="butt")
    ax.plot(m, i, "o", color="#0C1F40", ms=8)
    ax.text(hi + 40, i, f"{m:,.0f}  ({lo:,.0f} to {hi:,.0f})", va="center")
ax.set_yticks([0, 1], [r[0] for r in reversed(rows)])
ax.set_ylim(-0.6, 1.6); ax.set_xlim(2600, 4700)
ax.set_xlabel("KPI per unit of spend, with 90% interval")
ax.spines["left"].set_visible(False); ax.tick_params(axis="y", length=0); ax.grid(axis="x")
fig.tight_layout(); fig.savefig(HERE / "roi.png", dpi=200); plt.close(fig)

fig, ax = plt.subplots(figsize=(8, 3.4))
ax.plot(df.date_week, y, color="#9FAAE2", lw=1.4, label="Observed")
ax.plot(df.date_week, X @ b, color="#0C1F40", lw=1.6, label="Model")
ax.set_ylabel("KPI per week"); ax.grid(axis="y"); ax.legend(ncol=2, loc="upper left")
fig.tight_layout(); fig.savefig(HERE / "fit.png", dpi=200); plt.close(fig)

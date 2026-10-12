@app.cell(hide_code=True)
def _(header):
    header(
        "Marketing mix EDA, in PyMC style",
        "A style showcase on the pymc-marketing example dataset: header, key numbers, "
        "callouts, interactive controls, charts and rules-only tables.",
        badges=[("Style template", "teal"), ("Example data", "orange")],
        accent="PyMC",
    )
    return


@app.cell(hide_code=True)
def _(callout):
    callout(
        "**Data.** `mmm_example.csv` ships with pymc-marketing: 179 weeks of a simulated "
        "KPI `y`, two channel-spend series `x1`/`x2`, two events and a time index. It is a "
        "library example, not client data, so nothing here is a finding about a real business.",
        kind="warn",
    )
    return


@app.cell
def _(pd):
    df = pd.read_csv("data/mmm_example.csv", parse_dates=["date_week"])
    channels = ["x1", "x2"]
    return channels, df


@app.cell(hide_code=True)
def _(df, kpis):
    kpis([
        (f"{len(df)}", "weekly rows"),
        (f"{(df.date_week.max() - df.date_week.min()).days / 365.25:.1f} years", f"{df.date_week.min():%b %Y} to {df.date_week.max():%b %Y}"),
        (f"{df.y.mean():,.0f}", "mean weekly KPI (y)"),
        (f"{(df.x2 == 0).mean():.0%}", "weeks with x2 spend at zero"),
    ])
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## KPI over time

    Drag the window to zoom. The shaded bands mark the two event indicators in the data.
    """)
    return


@app.cell
def _(df, mo):
    window = mo.ui.range_slider(0, len(df) - 1, value=[0, len(df) - 1], full_width=True,
                                label="Week window")
    smooth = mo.ui.slider(1, 12, value=4, label="Rolling mean (weeks)")
    mo.hstack([window, smooth], widths=[2, 1], align="end")
    return smooth, window


@app.cell
def _(C, df, figure, plt, smooth, window):
    def kpi_chart():
        lo, hi = window.value
        d = df.iloc[lo:hi + 1]
        fig, ax = plt.subplots()
        ax.plot(d.date_week, d.y, color=C["periwinkle"], lw=1.2, label="Weekly KPI")
        ax.plot(d.date_week, d.y.rolling(smooth.value, min_periods=1).mean(),
                color=C["navy"], label=f"{smooth.value}-week rolling mean")
        for col, color, name in [("event_1", C["peach"], "Event 1"), ("event_2", C["aqua"], "Event 2")]:
            on = d[col] > 0
            ax.fill_between(d.date_week, 0, 1, where=on, color=color, alpha=0.55,
                            transform=ax.get_xaxis_transform(), label=name)
        ax.set_ylabel("KPI (y, units per week)")
        ax.set_title("The KPI trends upward with a yearly cycle")
        ax.grid(axis="y")
        ax.legend(ncol=4, loc="upper left", bbox_to_anchor=(0, -0.1))
        return figure(fig, "Simulated KPI from the pymc-marketing example data. Bands: event indicators.")
    kpi_chart()
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Do the channels move with the KPI?

    A correlation matrix is read in blocks, not skimmed for its largest cell. Diverging
    palette: **indigo** negative, **dark orange** positive, white at zero.
    """)
    return


@app.cell
def _(DIV, df, figure, np, plt):
    def corr_chart():
        cols = ["y", "x1", "x2", "event_1", "event_2", "t"]
        m = df[cols].corr(method="spearman")
        fig, ax = plt.subplots(figsize=(5.2, 4.2))
        im = ax.imshow(m.values, cmap=DIV, vmin=-1, vmax=1)
        ax.set_xticks(range(len(cols)), cols)
        ax.set_yticks(range(len(cols)), cols)
        for i in range(len(cols)):
            for j in range(len(cols)):
                v = m.values[i, j]
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8,
                        color="white" if abs(v) > 0.7 else "#0C1F40")
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_title("Spearman correlation")
        fig.colorbar(im, ax=ax, shrink=0.8, label="ρ")
        return figure(fig, "Spearman rank correlation, all 179 weeks.", dpi=170)
    corr_chart()
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## One channel at a time, with a lag

    Pick a channel and shift its spend by 0–8 weeks to see how the relationship with the
    KPI changes. Series colors follow the fixed cycle: a filter never repaints survivors.
    """)
    return


@app.cell
def _(channels, mo):
    pick = mo.ui.dropdown(channels, value="x1", label="Channel")
    lag = mo.ui.slider(0, 8, value=0, label="Lag (weeks)")
    mo.hstack([pick, lag], justify="start", gap=2)
    return lag, pick


@app.cell
def _(C, channels, df, figure, lag, pick, plt):
    def scatter_chart():
        color = {"x1": C["navy"], "x2": C["teal"]}[pick.value]
        x = df[pick.value].shift(lag.value)
        ok = x.notna()
        r = x[ok].corr(df.y[ok], method="spearman")
        fig, ax = plt.subplots(figsize=(6.2, 3.8))
        ax.scatter(x[ok], df.y[ok], s=34, color=color, alpha=0.85)
        ax.set_xlabel(f"{pick.value} spend, lagged {lag.value} wk (scaled units)")
        ax.set_ylabel("KPI (y)")
        ax.set_title(f"{pick.value} vs KPI at lag {lag.value}: Spearman ρ = {r:.2f}")
        ax.grid(True)
        return figure(fig, f"Each point is one week. n = {int(ok.sum())}.")
    scatter_chart()
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Summary table

    Rules only: heavy top and bottom, a light header rule, no vertical lines.
    """)
    return


@app.cell
def _(df, table):
    s = df[["y", "x1", "x2"]].describe().T[["count", "mean", "std", "min", "50%", "max"]]
    table(s.rename(columns={"50%": "median"}), fmt="{:,.2f}")
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ---
    *Built with the PyMC Labs artifact style for notebooks: Inter and JetBrains Mono, navy text, aqua rules,
    teal/indigo/dark-orange text accents, and the website's dark wordmark.*
    """)
    return

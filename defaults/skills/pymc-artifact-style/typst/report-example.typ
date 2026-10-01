#import "pymc-report.typ": *

#show: pymc-report.with(
  title: [Bayesian Price Elasticity for ACME Retail],
  subtitle: [Uncertainty-aware demand estimation and revenue-optimal
             pricing under partial information],
  client: [ACME Retail Group],
  author: [PyMC Labs],
  date: [June 2026],
  status: "Confidential",
  paper: "a4",                 // change to "us-letter" for US sizing
  cover-background: none,         // plain cover; use a path for supplied artwork
  abstract: [
    We estimate price elasticity of demand across ACME's twelve core SKUs
    using a hierarchical Bayesian model. The posterior implies a
    revenue-optimal average price increase of *4.2%* (90% credible interval
    2.1–6.4%), concentrated in low-elasticity categories. Unlike point
    estimates from the incumbent regression, our approach quantifies the
    risk of each pricing action, letting ACME cap the probability of a
    volume decline beyond 3% at a chosen tolerance.
  ],
)

= Background

ACME Retail asked us to revisit pricing across its core catalog. The
incumbent approach fits an ordinary least-squares regression of log-volume on
log-price and reports a single elasticity per category.#sidenote[
  The incumbent model also pools all stores, implicitly assuming identical
  price response, an assumption the data contradict (see @fig-shrinkage).
] That procedure produces a number, but no honest account of how *uncertain*
that number is. Pricing decisions made on overconfident estimates expose the
business to avoidable downside.

Our remit is therefore twofold:

- *Borrow strength.* Estimate elasticities that pool information across related
  categories, so sparse SKUs are not estimated in isolation.
- *Quantify risk.* Express every recommendation as a distribution over outcomes
  rather than a single point estimate, so each pricing action carries an explicit
  probability of downside.

This framing (partial pooling for estimation, posterior distributions for
decisions) carries through the rest of the report.

A text-width figure sits in the main column with its caption alongside in the
margin (@fig-textwidth).

#flowfigure(
  label: <fig-textwidth>,
  kind: image, supplement: [Figure],
  image("figures/demand.pdf", width: 100%),
  caption: [Weekly units versus price for a representative SKU (1 L cola) on the
    log scale. The posterior mean slope is the elasticity, and the shaded band is
    the 90% credible interval. #figtag("brisk-otter-lamp")],
)

= The model

#keyfigure[12][core SKUs modeled]

Let $q_(i t)$ denote units sold of SKU $i$ in week $t$ and $p_(i t)$ its price.
We model log-demand as a hierarchical linear function of log-price,

$ log q_(i t) = alpha_i + beta_i log p_(i t) + gamma^top x_(i t) + epsilon_(i t),
  quad epsilon_(i t) tilde cal(N)(0, sigma^2), $ <eq-demand>

where $beta_i$ is the elasticity of SKU $i$, $x_(i t)$ collects controls
(seasonality, promotions, stock-outs), and the SKU-level coefficients are
themselves drawn from a category-level prior,

$ beta_i tilde cal(N)(mu_(c(i)), tau^2_(c(i))). $ <eq-prior>

The partial pooling in @eq-prior is what lets sparse SKUs borrow information
from their category.#marginnote[
  *Why hierarchy?* A SKU with only a few price changes has little signal on its
  own. The category prior pulls its elasticity toward the group mean in
  proportion to how little it knows: automatic, principled shrinkage.
]
Posterior inference uses the No-U-Turn Sampler. Four chains of 2,000 draws
gave $hat(R) < 1.01$ for all parameters.

== Revenue-optimal price

Expected revenue for SKU $i$ at a candidate price $p$ follows from
@eq-demand by taking the expectation over the posterior predictive,

$ EE[R_i (p)] = p dot.c EE[q_i (p)]
  = p dot.c exp(alpha_i + beta_i log p + sigma^2 \/ 2). $

Maximizing over $p$ and propagating posterior uncertainty yields a
*distribution* of optimal prices per SKU, summarized in @tab-elasticity.

#flowfigure(
  label: <tab-elasticity>,
  dtable(
    columns: (auto, 1fr, auto, auto, auto),
    align: (left, left, right, right, right),
    table.header[Category][Example SKU][Elasticity\ $beta$][90% CI][Δ price],
    [Staples],     [2 kg flour],        [−0.42], [−0.61 … −0.24], [+6.1%],
    [Beverages],   [1 L cola],          [−0.88], [−1.05 … −0.71], [+3.4%],
    [Household],   [dish soap],         [−1.12], [−1.40 … −0.85], [+1.8%],
    [Premium],     [single-origin tea], [−1.95], [−2.41 … −1.52], [−0.7%],
  ),
  caption: [Posterior elasticities and revenue-optimal price moves. Low-elasticity
            staples carry the recommended increases. Premium lines are left flat.
            #figtag("amber-comet-vault")],
)

The pattern is intuitive: inelastic staples (small $|beta|$) tolerate price
increases, while elastic premium lines do not.

#marginfigure(
  label: <fig-shrinkage>,
  image("figures/shrinkage.pdf", width: 100%),
  kind: image, supplement: [Figure],
  caption: [Per-SKU elasticities shrink from their unpooled estimates (open
            circles) toward the category means (dashed) under partial pooling.
            The pull is strongest where data are sparse.
            #figtag("quiet-cedar-flux")],
)

= Recommendation

We recommend the price schedule in @tab-elasticity, phased over two quarters.

#keyfigure[+4.2%][optimal avg. price move]

#callout[
  *Headline.* Adopting the posterior-optimal schedule raises expected gross
  revenue by *4.2%* while holding the probability of a >3% volume decline below
  10%, a guarantee the incumbent point-estimate model cannot make.
]

Because every recommendation is a distribution, ACME can choose its own risk
tolerance: a more conservative cap simply trims the aggressive end of the
schedule, at a modest cost in expected upside.

#quotebox(by: [Category lead, ACME])[
  The credible intervals changed how we argue for price moves internally. We can
  finally state how confident we are in each move and set our own risk tolerance
  against it.
]

When a chart needs more room than the text column, it can span the full content
width (@fig-wide-demo), with the caption still set in the margin.

#widefigure(
  label: <fig-wide-demo>,
  kind: image, supplement: [Figure],
  image("figures/elasticity.pdf", width: 100%),
  caption: [Posterior elasticity for each SKU with its 90% credible interval,
    colored by category. Premium lines are the most elastic, staples the least.
    #figtag("ember-loom-crest")],
)

A single large exhibit can take a whole page of its own (@fig-fullpage-demo),
with a caption spanning the full page width.

#fullpagefigure(
  label: <fig-fullpage-demo>,
  kind: image, supplement: [Figure],
  image("figures/exhibit.pdf", width: 100%),
  caption: [Model exhibit: (a) fitted demand curves by category, (b) elasticity
    posteriors, (c) expected revenue versus price with the optimum marked, and
    (d) the posterior optimal price move per category.
    #figtag("slate-harbor-wren")],
)

#appendix("A", [Sampler diagnostics])

All parameters reached $hat(R) < 1.01$ with effective sample sizes above 1,200,
drawn with `pm.sample(2000, chains: 4)`. The hierarchical model is specified in
PyMC as:

```python
with pm.Model(coords=coords) as model:
    mu = pm.Normal("mu", 0.0, 1.0, dims="category")
    tau = pm.HalfNormal("tau", 1.0, dims="category")
    beta = pm.Normal("beta", mu[cat], tau[cat], dims="sku")
    sigma = pm.HalfNormal("sigma", 1.0)
    pm.Normal(
        "logq",
        alpha[sku] + beta[sku] * logp,
        sigma,
        observed=y,
    )
    idata = pm.sample(2000, chains=4, target_accept=0.9)
```

Trace plots and posterior predictive checks are available in the accompanying
notebook.

#appendix("B", [Notation])

#dtable(
  columns: (auto, 1fr),
  align: (left, left),
  table.header[Symbol][Meaning],
  [$q_(i t)$], [units of SKU $i$ sold in week $t$],
  [$p_(i t)$], [price of SKU $i$ in week $t$],
  [$beta_i$],  [price elasticity of SKU $i$],
  [$mu_c, tau_c$], [category-level prior mean and scale],
)

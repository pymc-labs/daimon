# Fit an MMM the way PyMC Labs would, in seven prompts

By the end of one thread you have priors checked before fitting, a fit that passes its convergence checks, and contributions and ROI read with their uncertainty.

The data is simulated and shaped like our project, so no Example Brokerage data is used. To run this on your own data, replace step 1 with your files; steps 2 to 7 stay the same.

Run the prompts in one thread, in order. Steps 4, 5 and 7 fit models, so start them before a meeting rather than during one. If Daimon has a different name in your channel, use that instead of `@daimon`.

## 1. Build and check the simulated data

```prompt
@daimon We're starting a short MMM walkthrough in this thread. Keep every file and model you make here so later steps can reuse them.

Build a simulated dataset shaped like ours, so no Example Brokerage data is used: 6 regions, 156 weekly rows each from January 2023, random seed 2027. Outcome: existing-asset net flows by region, with a few genuine outlier weeks. Media: national TV impressions with their cost, national upper-funnel spend, and paid search and paid social spend by region. Controls: seasonal event weeks and three one-off shock flags. Keep the true parameters out of the thread; we'll work as we would on real data.

Then check it like a real delivery (grain, gaps, week anchor, units, plausible ranges), flag the outlier weeks without removing them, and attach the cleaned files with a short data summary.
```

**You get:** Three years of weekly data for six regions, a pass/warn/fail data check, and cleaned files the later steps reuse.

Every number is made up; only the shape follows our project. The fixed seed means a rerun gives the same data.

## 2. Write down the model and its priors, before fitting

```prompt
@daimon Before fitting anything, write down the MMM you would fit to the cleaned files with PyMC-Marketing: multiplicative like our production model, region as a dimension, adstock and saturation on each paid channel, a slowly varying base (HSGP), and the event and shock flags as controls. If the outcome can go negative, say how you handle that before choosing the likelihood.

For every prior, give its range in business terms (for example, how many weeks an ad keeps working, or the ROI it implies for each channel) and say where it comes from. Show it as a table I can review. Don't fit yet.
```

**You get:** A one-page model specification with each prior explained in plain terms.

This is the step to argue about. Priors are where your team's knowledge goes in: past tests, benchmarks, and what you would call implausible. Changing them now is cheap; changing them after the fit means refitting.

## 3. Check the priors against reality (prior predictive check)

```prompt
@daimon Run a prior predictive check on that model: draw from the priors alone, with no data fitted. Plot the simulated net flows against the observed range for two regions, and show the channel contributions and ROI the priors imply.

Tell me whether the priors allow outcomes we would call impossible (media driving more than total flows, an ROI nobody would believe) or rule out plausible ones. Adjust what needs it, show before and after, and update the specification.
```

**You get:** Charts of what the model believes before it sees any data, and a revised prior table.

If the priors alone produce absurd outcomes, the fit will spend its effort fighting them, and the results inherit the problem.

## 4. Fit it properly and check convergence

```prompt
@daimon Fit the model with the revised priors: 4 chains, 1,000 tuning steps and 1,000 draws each (4,000 draws in total), target_accept 0.9, a fixed random seed.

Report convergence before any result: divergences (target zero), R-hat for every parameter (all at or below 1.01), and bulk and tail effective sample size (at least 400 each), with trace and rank plots for the worst few parameters. If any check fails, don't show contributions: say what failed and why, fix it (longer tuning, a reparameterisation or a better prior) and refit. Attach the fitted model.
```

**You get:** A convergence table and plots, and the fitted model that steps 5 to 7 reuse.

Four independent chains show whether separate runs agree, which is what R-hat measures. A thousand draws per chain usually gives enough effective samples to pin down the tails of a 94% interval as well as its middle. The checks, not the settings, decide whether that is enough: if ESS comes back low, draw more or fix the model. Passing them shows the sampler worked, not that the model is right; steps 5 and 7 test that. This is the slowest step so far.

## 5. Check the fit, in sample and on held-out weeks (posterior predictive check)

```prompt
@daimon Run a posterior predictive check: for each region, plot observed against predicted net flows with 50% and 94% intervals, report how often the observations fall inside the 94% band, and show residuals over time so we can see anything the model missed (event weeks, outlier weeks, the end of the sample).

Then refit with the same settings and checks on all but the last 13 weeks, forecast those 13 weeks, and report coverage and error on them. Say plainly where the model fits badly.
```

**You get:** Fit charts per region, interval coverage in sample and on the 13 held-out weeks, and a plain list of weak spots.

In-sample fit flatters every model; the held-out weeks are the fairer test. A good fit still does not prove the split between channels is right, because many splits fit the data about equally well. Step 6 shows how much that matters.

## 6. Read contributions and ROI with their uncertainty

```prompt
@daimon From the full fit, show each channel's contribution to net flows and its ROI over the whole period, each with a 94% interval, and each channel's response curve with its band and where current spend sits on it.

For each channel, compare prior and posterior ROI so we can see how much the data actually informed it, and flag channels the data cannot tell apart (strongly correlated posteriors). Lead with what we can and cannot say, in plain words.
```

**You get:** Contribution and ROI charts with intervals, response curves, and a short read of what the data supports.

Read the interval, not the point. A channel whose posterior looks like its prior is telling you about the prior, not the data. That channel is the first candidate for a real lift test.

## 7. Test the priors' influence and write the model card

```prompt
@daimon Refit once with the media priors twice as wide, using the same sampling settings and checks, and show which contributions and ROIs move.

Then write a one-page model card for our team: the model, the priors and why, every check with its result, what the model can and cannot tell us, and what would change our minds. Plain language, PyMC Labs style, attached as a PDF.
```

**You get:** A prior-sensitivity comparison and a one-page model card.

Large moves under wider priors mean the data is weak for that channel. That is a finding, not a failure, and it belongs in the model card. The model card is the first thing a model-risk reviewer will ask for.

## After the walkthrough, ask for any of these in the same thread

**Calibrate with a real lift test.** When a geo or lift test has actually run, add its result to the model as a calibration measurement and repeat steps 4 to 7. A test's result has to come from running the test; it cannot be simulated in advance. Daimon can help design the test: markets, length, spend change and the smallest effect it could detect.

**Brand equity model.** The stage-2 Bayesian VAR on the stage-1 base, awareness and consideration. The same checks apply: a prior predictive check for explosive dynamics, the same convergence checks, and enough stage-1 draws carried over that the brand equity interval stops moving when more are added. A few dozen draws is a smoke test, not a result.

**MTA reconciliation.** Where MTA credit and the MMM disagree, and clear rules for which source to trust for which decision.

**Budget planner.** Scenarios and budget optimisation on the fitted response curves, with spend limits. Only once the model has passed the checks above.

**One-pager for a VP.** The model card rewritten as three short paragraphs for leadership, with uncertainty on every number.

**Your team's own agent.** An agent that reruns these checks when new weekly files arrive and follows your conventions. Setting one up needs an admin.

## Questions

### Is any Example Brokerage data used?

No. Daimon creates every number in step 1. The shape follows our project; the values are made up, so results describe the simulated data, not your business.

### Does this carry over to our real data?

Yes. Replace step 1 with your own files and ask Daimon to check them like a real delivery; steps 2 to 7 stay the same. Comparing estimates with the values that generated the data is possible only because this data is simulated, and does not carry over to real data, which is why this walkthrough relies on the checks instead.

### Who checks the answers?

Daimon shows every check it runs. Before a number goes into a decision, bring it to the coaching call or send it to your PyMC Labs contacts. After any step, ask “what did you assume and not run?” to see where the answer stops.

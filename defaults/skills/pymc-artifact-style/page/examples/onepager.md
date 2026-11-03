# Keep funding Channel A; test Channel B before moving budget to it

Each unit of spend on Channel A returned about 3,600 KPI (90% interval 3,300 to 3,950), and Channel B about 3,000 (2,800 to 3,250). Channel A drives about a fifth of the KPI.

| Channel | Return per unit | Weeks of data |
|---|---|---|
| Channel A | 3,621 | 179 |
| Channel B | 3,036 | 32 |

## Channel B's number rests on 32 weeks of data

Channel B was off in 82% of weeks, so its number rests on 32 weeks. The model is a quick regression with fixed carry-over and no saturation, so returns could fall at higher spend and it would not show.

## Approve a geo test on Channel B and the full model

A 6-week geo test on Channel B, with no spend change elsewhere, and two weeks for the full Bayesian MMM.

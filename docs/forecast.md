# Liquid-cash forecast

`backend/app/formulas/forecast.py` forecasts a customer's liquid cash (checking + savings) day by day for up to 24 months, with an
uncertainty band and two risk probabilities. This page says what it does, how accurate it is, and what it does not claim.

## The pitch line

> **On customers the model had never seen, the 80% band contained the actual outcome 82% of the time at month-end (84% of days).
> The typical customer's six-month error is about 6.2% of one month's spending.**

Supporting numbers, all from the backtest (train on the first 18 months, forecast the last 6, 98 customers):

| Horizon | Typical customer's error (median) | 75th percentile | 90th percentile |
|---|---|---|---|
| 30 days | 3.9% | 6.8% | 14.8% |
| 90 days | 5.8% | 8.1% | 10.9% |
| 180 days | 6.2% | 10.8% | 17.1% |

"Error" is each customer's *stricter* of two measures, as a share of that customer's average monthly outflow: the mean daily gap
between the forecast median and what happened, and the gap in cumulative net change at the horizon. Quote the stricter one. We do
not quote MAPE (1.8% at 180 days): it divides by the balance, which sits near zero for many customers, and flatters the model.

Risk forecasts, on held-out customers (see the reliability table below): of the 87 customers forecast under 10% to fall below a
one-month cushion, none did; of the 6 forecast at 60% or more, all 6 did.

## What it produces

`forecast_liquid(profile, ...)` returns a `LiquidForecast`; `forecast_liquid_summary` is the same without the `daily` and `monthly`
arrays, for agents.

- `daily`: p10 / p50 / p90 for days 0-90. `monthly`: the same at each month-end for the whole horizon.
- `overdraft_risk`: probability that liquid cash drops below **$0** (a true emergency: the urgent alert).
- `cushion_risk`: probability it drops below **one month of essential expenses** (housing, utilities, insurance, loan payments,
  groceries, healthcare, transport). This is the default indicator for the health score ("you'll dip below a safe cushion in March").
  Each risk has `probability`, `probability_90d`, and the first date the median / bad-luck (p10) path crosses the line.
- `low_point`, `monthly_net`, `drivers` (month-1 cash by kind), `events` (raises, rent renewals, loans ending, CD maturities),
  `assumptions` (every value used, with its source), `backtest`, and a `calibration` note describing how the bands were scaled.
- `changes=[ForecastChange(...)]` applies a what-if. Only `lump_sum` and `income_change` are implemented; the other three types
  raise `NotImplementedError`.

A 24-month, 1,000-path forecast takes about 18 ms (median over customers).

## How it works

1. **Schedule (deterministic).** Recurring flows detected from history are placed on real dates: paychecks move to the previous
   business day, bills and loan payments to the next one. Raises (or the Social Security COLA for retirees) and rent renewals apply
   on each anniversary of the as-of date; other bills inflate monthly; loan payments never change. A loan stops after its remaining
   payments, with a `loan_ends` event. CDs only get a `cd_matures` marker (they auto-renew outside liquid cash).
2. **Variable spend (stochastic).** Each path draws months from the customer's own history in runs of 2-3 consecutive months,
   multiplies by a seasonal index and inflation, and spreads the month evenly over its days. Each path also gets its own
   plausible mean spending level, a slow drift, and its own seasonal factors (wider uncertainty for calendar months observed once).
3. **Card.** With a card, the draw is *total* spend. The checking share leaves daily; the card share builds a balance that is paid
   on the customer's usual payment date. Pay-in-full cards pay the whole balance; revolvers accrue interest and pay new charges plus
   interest plus a share of the old balance (the share is read off how fast the balance fell in the history).
4. **Noisy bills.** Utilities and other bills whose amount varies by 2% or more month to month are resampled from their own
   history, using the same month draws as spending so their real co-movement is kept. Rent, loans and subscriptions stay fixed.
5. **Sweeps.** A customer who moves surplus out of checking to an outside account gets a rule learned from their own sweeps (move
   the excess over a target once checking passes a threshold, on their usual day).
6. **Savings interest** is added at each month-end. Balances are **not** clamped at zero: the point is to show shortfalls.
7. **Calibration.** The raw bands are over-confident (the actual outcome fell inside them only 60% of the time at month-end, 43%
   of days). For each horizon (30 / 90 / 180 days) we learn a factor that scales deviations from the median so the band contains
   the outcome 80% of the time (`scripts/fit_calibration.py`, saved in `backend/app/calibration.json`). Two factor sets: one for the
   daily chart, one for the monthly chart and the risk probabilities. The same scaling is applied to the simulated paths before the
   risk probabilities are computed.

Seeds are derived from a stable hash of the profile id and the arguments, so the same call gives identical output.

**Reading a single customer's `backtest` block:** its `band_coverage` figures are for the *raw* bands, before calibration (C001's own
month-end coverage there is 0.33 even though the bands it is shown with are calibrated). Calibration is only meaningful in
aggregate, across customers; the held-out numbers below are the ones to quote.

## Held-out check, and how to read it

Customers were split into two halves; factors fitted on one half were scored on the other (both ways).

| 80% band coverage | Month-end | Every day |
|---|---|---|
| raw model | 60% | 43% |
| calibrated, held-out customers | **82%** | **84%** |

Risk reliability, each held-out customer scored with factors fitted on the other half. "Happened" for `overdraft_risk` means the
ledger had to top checking up from an outside account (the generator never lets the balance go below $0, so the event is observed
that way); for `cushion_risk` it means liquid cash fell below one month of essentials at any point in the 6 test months.

| `overdraft_risk` | n | mean forecast | happened |
|---|---|---|---|
| 0% | 96 | 0.00 | 0 / 96 |
| 30-60% | 1 | 0.31 | 1 / 1 |
| 60-100% | 1 | 0.99 | 1 / 1 |
| *all (calibrated / raw)* | 98 | *0.013 / 0.012* | *observed rate 0.020* |

| `cushion_risk` | n | mean forecast | happened |
|---|---|---|---|
| 0% | 84 | 0.00 | 0 / 84 |
| 0-10% | 3 | 0.02 | 0 / 3 |
| 30-60% | 2 | 0.46 | 1 / 2 |
| 60-100% | 6 | 0.94 | 6 / 6 |
| *all (calibrated / raw)* | 95 | *0.070 / 0.068* | *observed rate 0.074* |

Brier score improved from 0.0076 to 0.0049 (overdraft) and from 0.0118 to 0.0090 (cushion) with calibrated paths. Three customers
who were already below a cushion at the cutoff are left out of the cushion table.

**Read this as discrimination, not fine calibration.** Only 2 customers needed outside money and 7 fell below a cushion. Almost
everyone sits at 0% or near 100%, so the middle bins hold one to three customers each. The table supports "customers forecast safe
did not hit trouble, and customers forecast at high risk did." It does not support a claim that a 30% forecast means 30%.

## Caveats

- **Held-out customers share the same six test months.** Any shock common to everyone (an earlier version of the model ran about
  4% of outflow too high from July for nearly all customers; the cause was never identified and I did not re-measure it after the
  later changes) is learned by the calibration. This shows the model generalizes
  across customers, not across time; with 24 months of data an out-of-time test is not possible.
- **Calibration covers 6 months.** Beyond 180 days the factor is held flat. The 24-month chart's wider horizon is an extrapolation.
- **The backtest is slightly pessimistic.** May-October appear once in the 18-month training window, so each test month's seasonal
  factor rests on one observation. The shipped model fits on all 24 months (two observations per month).
- **The daily chart's factor is large at short horizons (about x8 at 30 days).** The model spreads spending evenly across a month, while
  real spending is lumpy, so daily bands are far too narrow before scaling. Transaction-level timing is not built yet; when it
  is, refit the calibration (the factor should shrink).
- **Customers who opened a CD in the test window (C019, C082) are excluded** from calibration and coverage: nobody can forecast that.
- **Re-run `scripts/fit_calibration.py` after any change** to the model, data or assumptions. `check_forecast.py` fails if the saved
  factors are more than 10% off a refit.
- **Synthetic data.** Spending months in the generator are independent and its customers follow its own rules, so some findings
  (no measurable spending drift beyond 0.6% a year; sweeps recovered at exactly 3.5x / 2.5x of income) may not transfer. The
  methods do not depend on the generator; the numbers might.
- **Assumptions** (`backend/app/assumptions.json`, each with source and date): inflation 3.15% (average of BLS CPI-U headline 3.7% and
  core 2.6%, 12 months to Sep 2026; headline alone is energy-driven and would overstate a 24-month horizon), wage growth 3.1% (BLS ECI),
  Social Security COLA 3.6% (SSA, applied on the as-of anniversary, not in January), rent renewal 2.8% (BLS national rent; regional
  figures are higher, Midwest 4.1%), 1,000 paths. Update them before relying on the forecast outside the demo.
- **Revolving-card payments fall over time** as the simulated balance is paid down, so a revolver's cash flow improves relative to
  its history. That is intended, but it rests on the paydown rate read from the history.
- **Outside-money top-ups are not simulated.** The forecast shows the shortfall instead of covering it.

## Maintenance

| Task | Command |
|---|---|
| Check the model against the ledger and the calibration | `.venv/bin/python3 scripts/check_forecast.py` |
| Re-measure coverage as each variance source is switched on | `.venv/bin/python3 scripts/measure_coverage.py` |
| Refit band calibration | `.venv/bin/python3 scripts/fit_calibration.py` |

Model switches live in `forecast.OPTIONS`; all are on in production.

## Calendar events from the schedule

Computed from the schedule, not guessed, and listed in `events` for the UI and the agents:

- `extra_paycheck`: a calendar month with three biweekly paydays; `monthly_impact` is one paycheck. Two a year for every biweekly earner.
- `double_payment`: a monthly bill or card payment that slid across the month boundary and lands twice (e.g. Feb 28 falling on a Saturday
  moves to Mar 1, so March has two card payments); `monthly_impact` is the extra payment (card: the simulated median).
- `raise` and `rent_renewal` are placed on the as-of anniversary. That timing is assumed, and the debug page labels it.

## Utility seasonality (tested, off)

`derive.py` can learn 12 calendar-month factors per utility bill (`Recurring.month_factors`, shrunk and rescaled to average 1.0) and the
forecast applies them. Backtest, 180-day median error / raw month-end coverage: no seasonality 6.2% / 0.60; half the observed swing
6.8% / 0.58; all of it 7.1% / 0.55. It made the forecast worse, so `UTILITY_SEASON_SHRINK = 0.0` (off) and the calibration is unchanged.
Revisit once the unexplained July over-forecast is understood.

## Emergency fund and job-loss stress test (`job_loss_stress`)

Runs the liquid-cash forecast three times on the same random draws (a paired comparison): unchanged (`baseline`); income following
a monthly employed / unemployed Markov chain (`markov`); and the paycheck stopping next month with no new job (`lost_job_now`).
While unemployed the customer gets Pennsylvania unemployment benefits for 6 months (half of gross pay, capped), then nothing.
The `emergency_fund` block gives months of essentials covered, the gap to a 3-month target, and months to close it at the current surplus.
Retirees return `applicable: false` (no paycheck to lose).

Rates and their sources are in `assumptions.json`: job loss 0.89% a month (BLS CPS flows, Jun-Oct 2026); re-employment 16% a month,
chosen to match BLS mean unemployment duration (27.1 weeks), not the raw 25% flow rate, so a long job loss is not understated.
Caveats: the PA weekly maximum ($604) comes from an undated chart and is unconfirmed; benefits are treated as take-home; a new job restores
full pay immediately; the chain is memoryless, so it cannot match both the mean and the median spell length.

C002 (29, renter): 1.7 months of essentials covered (gap $2,866, about 3.3 months of surplus to close it). Overdraft probability over 24
months: 0% baseline, 8% with the Markov chain, 100% if the job is lost now (median path overdrawn about 2.3 months after the loss).

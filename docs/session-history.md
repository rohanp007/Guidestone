# Session history: Guidestone (PNC 360 Financial Health Navigator)

A reconstruction of the working session, 2026-11-07, between Rohan (user) and Claude Code. It is a faithful log of what was asked, decided,
built and measured, written from the session itself. It is **not a verbatim transcript**: tool output and pasted specs are summarized, and
numbers are the ones the session produced. File paths are relative to the repo root.

---

## 1. Outline of the build

| # | What | Outcome |
|---|---|---|
| 1 | Scaffold the folder skeleton | `data/`, `scripts/`, `backend/app/{data,formulas,agent}`, FastAPI stub, Next.js frontend |
| 2 | `requirements.txt` | One root file for scripts and backend; rule agreed: add every new dependency as the code is written |
| 3 | Review `scripts/generate_bank_data.py` | Deterministic, ledger exact; ~10 bugs and design issues found, 9 fixed |
| 4 | `scripts/to_parquet.py` | Typed Parquet for 7 CSVs (decimal money, date32 dates, string ids) |
| 5 | `models.py` contract + `loader.py` | `Profile` contract; validated loader, 100 profiles |
| 6 | Bio side file, estimated home / vehicle assets | `generate_bio.py`, `generate_assets.py`; `home` / `vehicle` account types |
| 7 | `derive.py` | recurring flows, variable spend, history, `SpendingProfile`; `check_derive.py` |
| 8 | Insight + load speed | `paycheck_to_paycheck`; profile pickle cache (cold 1.9 s, warm 0.23 s) |
| 9 | Liquid-cash forecast engine | Monte Carlo, schedule + stochastic spend, backtest, `assumptions.json` with sources |
| 10 | Calibrating the bands | Seven variance sources added and measured; split-conformal calibration |
| 11 | Risk probabilities + reliability | Calibrated paths; reliability table on held-out customers; `docs/forecast.md` |
| 12 | Minimal API | 7 endpoints, 24 tests, generated TypeScript types; `docs/api.md` |

Current verification state: `check_derive.py` passes, `check_forecast.py` passes, `pytest` 24 passed, API ready in 1.06 s.

---

## 2. Chronological log

### 2.1 Scaffold and requirements
- Created the skeleton from the pasted tree. The repo was empty (just `.git`). Added `.gitignore`, stubs for the generator and
  validator, `registry.py` (`@register` decorator), a FastAPI `/health` stub, and a Next.js app via `create-next-app --skip-install`.
- Requirements: first a backend file, then consolidated to one root `requirements.txt`. Saved a memory note: keep it updated.
- Environment wrinkle: `pip` (pyenv 3.13) and `python3` (Homebrew 3.14) were different interpreters, and an existing `.venv` was
  built on 3.13. Ran everything with `.venv/bin/python3.14`. Left the venv alone (it may be the user's).

### 2.2 Generator review (user wrote the full 630-line generator)
Ran it with 300 customers: 384k transactions in 2.5 s, byte-identical across runs, ledger reconciles exactly, transfers paired, no
negative balances. Findings:
- **Fixed:** card payment could overpay below $35 (`clip` with `lo > hi`); CSV files never closed (now `ExitStack`); `--out` default
  depended on the working directory; `cd_rate` parameter shadowed by a nested function (renamed `cd_share`); duplicate `if/else`
  branch; IRA contribution could exceed the limit (cap now the minimum across `IRA_LIMIT`, round down); equity weight could push
  bond weight negative; `plug_out` counted but never written; stale README comment; `pandas`/`numpy` removed from requirements
  (script is stdlib-only).
- **Design issues flagged, not fixed:** no financial-distress cases (the generator tops up checking so the balance never goes
  negative, so lowest checking balance is $0.01); outside-account top-ups and sweeps move money in and out of nowhere; a 400-line
  `build_customer`; some source figures not verifiable (SCF 2025, FDIC Sep 2026, a 2026 mortgage rate); insurer description
  chosen randomly every month (so the insurer name is unreliable downstream).
- Side effect of the IRA fixes: same seed gives 383,880 instead of 383,876 transactions.

### 2.3 Parquet, contract, loader
- `to_parquet.py`: explicit schema per file; money as `decimal128(14,2)`; empty cells become nulls; header mismatch fails loudly.
- `models.py` (user supplied the contract): checked against the real data. Matched on enums, ranges, balances. Added
  `overdraft_plug_withdrawals`; user later added `monthly_non_debt_expenses` and raised `target_age` to 90 (3 retirees were over 80).
- Data gaps: `state`, `dependents`, 401(k) fields had no source. User specified `generate_bio.py` (deterministic, tied to age,
  income, housing); result: 84 PA, 31 customers with kids (17 with a kid 10-17), 58 of 83 employed with a 401(k).
- `loader.py`: one cached read of all tables, grouped by customer once; null normalizer; builds `Account`/`Profile`; collects every
  validation error and raises once; `Summary` built from the finished profile. Hand-checked C001 against raw CSVs (every number tied).
  100 profiles in ~0.5 s.
- User caught the real problem the check exposed: homes and cars counted as $0 assets. Added `scripts/generate_assets.py`
  (home from loan / 0.8 LTV appreciating 3% a year, or 2-4.5x income; vehicle depreciating 20% then 12%). C001 net worth went from
  -$58.1k to +$35.5k; only 1 of 100 customers still negative. Estimates never touch `liquid_cash`.

### 2.4 `derive.py`
- **Recurring flows** detected from checking, seen in 10+ of 24 months, in a fixed set of categories (merchant spend is excluded:
  it hits many months but is discretionary). Insurance and payroll grouped by category because the description is unstable.
  Added `kind`, `cadence`, `last_date`; ended flows (paid-off loans) are dropped, found because the first liquid-flow tie-out failed
  for loans that had ended mid-history.
- `monthly_amount` changed from median x count to **observed average per month**, with occurrences over first-to-last month: the
  card payment sliding from Sat Feb 28 to Mar 2 had inflated the count, and the median is biased for skewed payoffs.
- `variable_spend` (checking + card) and `variable_spend_checking`; `history` (24 points, home/vehicle interpolated).
- Three checks (`scripts/check_derive.py`): expense identity within 0.11%, liquid cash-flow tie-out within 0.06% of income,
  history ties to the loader's `Summary`. `IMPLEMENTED = True` turns on the "every profile has a recurring inflow" load check.

### 2.5 `SpendingProfile`
Seasonality (shrunk halfway to 1.0; December shopping median 1.155 against the generator's raw 1.35, as predicted), volatility, trend,
deseasonalized monthly samples for the Monte Carlo, card share, carry months, subscriptions, sweeps and pulls, buffer ratio, top
categories. Decisions:
- Trend threshold: user set `TREND_MIN_T = 2.0`; a practical filter (>=10% a year **and** >=$25 a month) produces `notable_trends`.
  35% of series reported a trend at 1 sigma on data with no true trend. Five healthcare trends survive; a coefficient-of-variation gate
  at 0.6 would have removed none of them (their CV is 0.13-0.19), so it was skipped with the evidence. Even 3 sigma leaves two.
- `paycheck_to_paycheck` (in `formulas/health.py`): top-ups 0.45, near-zero buffer 0.35 (cutoff 0.02 sits in a natural gap: 17
  customers below 0.007, next at 0.026), soft signals 0.10 each; flag at 0.5. 8 of 100 flagged. All 5 top-up customers have a
  near-zero buffer, but only 5 of the 17 near-zero customers needed top-ups (the rest drew on savings).
- Load time: `_spending` was the slow part; `pivot_table` and per-row lambdas, not seasonality, were the cost. Replaced with
  `groupby().unstack()` and vectorized operations (2.4 s to 2.0 s), then the profile cache keyed on file timestamps/sizes and the code
  (cold 1.9 s, warm 0.23 s). The $20/month seasonality skip was added as requested but was not the cause.

### 2.6 Forecast engine (`formulas/forecast.py`)
Built per the user's brief: deterministic schedule of dated cash events (paychecks to the previous business day, bills to the next),
escalation (raises or COLA, rent renewal, inflation), loan endings, CD maturity markers; bootstrap of the customer's own variable
spend; month-end savings interest; no clamping at zero; stable-hash seeds; cached backtest; two registered formulas.
- **Assumptions with sources** (`backend/app/assumptions.json`): inflation 3.15% (average of BLS CPI-U headline 3.7% and core 2.6%,
  12 months to Sep 2026, chosen because energy-driven spikes fade), wage growth 3.1% (BLS ECI), Social Security COLA 3.6% (SSA, 2027),
  rent renewal 2.8% (BLS national rent; regional is higher), 1,000 paths.
- **Checks departed from the spec in three places**, each reported: (1) C001 "within 10% of history" cannot hold with inflation on
  (history is +$2.5 a month) so the engine is tested with escalation off against history minus one-offs; (2) the ending-loan step is
  measured by running the forecast with and without the loan on the same seed, because raw month-to-month p50 swings by thousands
  when a card payment slides over a weekend; (3) shortfall is judged at a buffer of one month of expenses, since at $0 only 6 of 100
  customers ever go negative.
- Added `overdraft_risk` ($0) and `cushion_risk` (one month of essentials: housing, utilities, insurance, loan payments, groceries,
  healthcare, transport) as separate outputs; cushion is the default health indicator.
- Block bootstrap (2-3 months), made circular after it was found to under-weight the first and last months.
- Honest error metrics added: error as a share of one month's outflow, stricter of mean daily error and cumulative-net-change error,
  taken per customer then summarized. MAPE is reported but not quoted (it divides by balances near zero).

### 2.7 Calibrating the bands
The initial backtest (18 months train, 6 test, 98 customers; two who opened CDs excluded) gave median coverage of the 80% band of 0.71
by luck of averaging: card holders 0.80 (bands inflated by the generator's random card/checking routing), no-card customers 0.42.
Diagnostics, in order:
1. Month-end vs every-day coverage, plus signed bias for the 34 low-coverage customers: month-end was also low and bias was
   one-sided only for ~6 customers (external sweeps, one CD). Hypothesis "even daily spread" was **not** the main cause.
2. Further cuts found the real cause: low-variance spending (no-card customers' monthly variable spend has a coefficient of variation
   of 2.9% against 25.6% for card holders) gave bands about 3x narrower while errors were the same size.
3. Steps, re-measured cumulatively (month-end coverage / 180-day error):

   | Step | Coverage | Error at 180d |
   |---|---|---|
   | baseline | 0.71 | 10.7% |
   | 1: total spend + simulated card balance | 0.43 | 6.5% |
   | 2: parameter uncertainty + drift | 0.47 | 6.6% |
   | 3: noise in varying bills | 0.52 | 6.6% |
   | 2b: seasonal-factor uncertainty | 0.59 | 6.5% |
   | sweep rule (learned from the customer's own sweeps) | 0.60 | 6.2% |

   Step 1 cut coverage but improved accuracy about 40%. Drift measured at only ~0.6% a year, so it was built in at that size.
   The sweep rule recovered the generator's 3.5x / 2.5x pattern without being told. A seasonal-shrinkage test showed less shrinkage
   hurts; no seasonality gives better coverage but worse accuracy, which led to step 2b.
4. **Split-conformal calibration** (`scripts/fit_calibration.py`, `backend/app/calibration.json`): one factor per horizon (30/90/180
   days), separate sets for the monthly and daily charts. Fit on one half of customers and tested on the other: monthly band
   **82%** (raw 60%), daily band **84%** (raw 43%). The daily factor is ~x8 at 30 days, a symptom of the missing within-month timing.
5. Risk probabilities now computed on calibrated paths (deviations from the median scaled by the interpolated month-end factor).
   Reliability on held-out customers: 0 of 96 customers forecast at zero overdraft risk needed outside money; of 87 forecast under 10%
   cushion risk, none fell below a cushion; of 6 forecast at 60%+, all 6 did. Only 2 overdraft events and 7 cushion events exist, so this
   shows discrimination, not fine calibration.
- Pitch line (in `docs/forecast.md`): on held-out customers the 80% band contained the actual outcome 82% of the time at month-end; the
  typical customer's six-month error is about 6.2% of a month's spending (3.9% at 30 days, 5.8% at 90; 90th percentile 17%).

### 2.8 The API (last task)
Built to the brief: `app/main.py` (lifespan preload, CORS for `http://localhost:3000`, central error handler), `api/{deps,profiles,
formulas,meta,schemas,serialize}.py`.
- Seven endpoints: health, profiles, profile detail, transactions, formulas, run formula, assumptions.
- Registry extended: each formula declares an args model and result model; the schemas are exposed for the debug page and agents.
- A strict JSON helper (`serialize.py`) sanitizes numpy / pandas / NaN; it is the default response class.
- Formula results cached by (profile, formula, arguments); `X-Compute-Ms` and `X-Cache` headers.
- 24 tests in `backend/tests/test_api.py`, including a strict-JSON round trip of every formula for 5 customers.
- Bugs the tests and type checks caught: transaction dates serialized as timestamps instead of dates; formula args/result models absent
  from the generated TypeScript types (fixed by publishing them in the OpenAPI schema); defaulted request fields generated as required
  (fixed with `--default-non-nullable false`).
- Ready in 1.06 s with warm caches.

---

## 3. Mistakes made and corrected along the way
- Assumed pip and python3 were the same interpreter (they were not); a quiet `grep -v` hid a failed `pip install pytest`.
- Used a zsh variable as a multi-word command (silently ran nothing); named a DataFrame column `cov` (shadowed a method).
- Three of my own check scripts asserted the wrong thing and were fixed with the reason stated: the shortfall buffer, the loan-step
  sign and tolerance, and a "history tie" that ignored paid-down card balances and ended loans.
- Wrong hypotheses, tested and dropped: even daily spend as the cause of low coverage; seasonal shrinkage as the cause of the July
  shift; the volatility gate as a fix for noisy trends.
- Used `/tmp` for a server log and PID file once, against the session's guidance on scratch space (removed afterwards).

## 4. Open items and caveats
- **`frontend/` is empty.** The Next.js scaffold from the start is gone (no `package.json`); I did not recreate it. The generated
  `frontend/src/lib/api-types.ts` and `api.ts` are there and type-check strictly (with a negative control), but the `gen:api` script could
  not be added. Add `"gen:api": "openapi-typescript http://localhost:8000/openapi.json --default-non-nullable false -o src/lib/api-types.ts"`.
- Transaction-level spending timing (step 5) is not built; the daily chart's x8 factor stands in for it, and the calibration must be
  refit afterwards.
- Calibration covers six months; beyond 180 days the factor is held flat. Held-out customers share the same test calendar, so this
  validates across customers, not across time.
- The July shift (an earlier model ran ~4% of outflow too high from July for nearly everyone) was never explained.
- `ForecastChange` types `extra_monthly`, `add_loan`, `expense_change` raise `NotImplementedError` (HTTP 501).
- A single customer's `backtest` block reports raw (uncalibrated) coverage; only the aggregate held-out numbers should be quoted.
- Synthetic data: some findings (no spending drift, sweeps at exactly 3.5x / 2.5x) may not transfer; the methods should.
- Insurer name in recurring flows is unreliable because of the generator; a rolling-origin backtest would test the risk forecasts better.
- Nothing is committed; git status shows the whole project untracked or modified.

## 5. Where things are
- Generators and tools: `scripts/` (`generate_bank_data.py`, `to_parquet.py`, `generate_bio.py`, `generate_assets.py`,
  `check_derive.py`, `check_forecast.py`, `measure_coverage.py`, `fit_calibration.py`).
- Backend: `backend/app/{config.py, assumptions.json, calibration.json, main.py}`, `data/{models,loader,derive}.py`,
  `formulas/{registry,health,forecast}.py`, `api/`; tests in `backend/tests/`.
- Docs: `docs/forecast.md` (method, pitch line, caveats), `docs/api.md`, this file.
- Pipeline order: generator, `to_parquet.py`, `generate_bio.py`, `generate_assets.py`, then `fit_calibration.py` after any model change.

# Financial decision rules

These rules decide **what a customer should do next**. They are implemented as deterministic,
registered formulas (`priority_waterfall`, `payoff_priority`, `candidate_moves`). The advisor
agent **explains and phrases** their output; it never overrides it, invents a priority, or adds a
move that is not in the candidate list.

All thresholds live in `backend/app/assumptions.json` with a source or rationale (see the
table at the end). Code reads them from there; no magic numbers in formulas.

Definitions used throughout:
- **Liquid cash**: checking + savings balances. CDs, IRAs, home and vehicle are not liquid.
- **Essentials (monthly)**: housing, utilities, insurance, loan minimums, groceries, healthcare,
  transport — the same "one month of essentials" used for the forecast cushion.
- **Surplus (monthly)**: net income − non-debt expenses − debt minimum payments.
- **Safe savings rate**: the high-yield savings rate in `assumptions.json`.

---

## 1. Priority waterfall — `priority_waterfall(profile)`

Orders where each dollar of **monthly surplus** should go.

Returns an ordered list of:
```
{ step, status, target, current, gap, monthly_suggested, reason_code }
status ∈ done | in_progress | todo | not_applicable | unknown
```

| # | Step | Target / condition | reason_code |
|---|------|--------------------|-------------|
| 1 | Starter emergency fund | liquid cash ≥ $1,000, or 1 month of essentials if that is less | `no_buffer` |
| 2 | Employer 401(k) match | if `has_401k` and `employer_match_pct > 0`: contribute enough for the full match | `free_money` |
| 3 | High-interest debt | every debt with APR ≥ 8%, highest APR first (avalanche) | `high_apr` |
| 4 | Full emergency fund | liquid cash ≥ 3 months of essentials; **6 months** if the paycheck-to-paycheck flag is set or there are dependents under 18. Retirees: `done` when retirement/investment accounts cover ≥ 12 months of essentials (drawdown backstop) | `cushion` |
| 5 | Retirement | total retirement saving toward 15% of gross income (employer match counts when known); retirees: `not_applicable` | `retirement_gap` |
| 6 | Education | only if dependents under 18 exist **and** steps 1–5 are done or in progress | `education` |
| 7 | Moderate-APR debt or extra investing | debt with safe savings rate ≤ APR < 8%: extra payments optional; APR below the safe savings rate: `not_applicable` (pay the minimum) | `optional` |

Rules:
- A step is `done` when its target is met.
- The **first step that is not done receives the surplus**. If the surplus exceeds that step's
  gap, the remainder flows to the next step not done. `monthly_suggested` records the split.
- **`unknown` does not block the order.** Step 2 is `unknown` because the dataset has no 401(k)
  contribution data: show a "confirm your 401(k) match" prompt and let the surplus continue.
- Step 5 counts IRA transfers observed in the 24-month history. 401(k) contributions are unknown.
- **Retirement comes before education, always.** When both apply, the advisor says so plainly.
- Surplus left after steps 1–6 flows to step 7 (extra investing when there is no moderate-APR debt); `unallocated` is only what remains after that.
- If surplus ≤ 0, every `monthly_suggested` is 0 and the waterfall reports the current step only.
- Future work (no data yet): self-employment and single-earner household as 6-month triggers.

---

## 2. Payoff priority — `payoff_priority(profile, lump_sum=0, extra_monthly=None, use_cd=False)` (`None` = use the monthly surplus; `0` = no extra)

Orders debts for extra payments, computes the safe lump sum, and raises flags.

Returns:
```
{ order: [{account_id, apr, balance, reason_code}], safe_lump_sum, safe_lump_sum_low_cushion,
  snowball_extra_cost, cd: {...} | null, flags: [...] }
```

1. **Cushion floor for lump sums.** A lump sum from existing cash never takes liquid cash below
   3 months of essentials:
   `safe_lump_sum = max(0, liquid − 3 × essentials)`.
   - Exception: when paying **cards with APR ≥ 20%**, allow down to 1 month of essentials:
     `safe_lump_sum_low_cushion = max(0, liquid − 1 × essentials)`, flag `tradeoff_low_cushion`,
     and show both versions.
2. **Ordering.** Debts with APR ≥ 8% first, highest APR first (avalanche). **At equal APR, cards
   come before installment loans** (paying cards also lowers utilization and helps the score).
3. **Keep cash when it earns more.** Debt with APR below the safe savings rate gets no extra-payment
   recommendation; flag `keep_cash_earns_more`.
4. **Mortgage excluded by default.** Include it only if its rate > 6.5% **and** waterfall steps
   1–5 are done.
5. **Freed payments roll over.** When a debt is paid off, its minimum payment goes to the next debt
   in the order (this is what makes avalanche and snowball work; the simulator must model it).
6. **CD as a funding source** (only when the user selects it, `use_cd=True`):
   - penalty = `penalty_days × cd_balance × cd_rate / 365`
   - net benefit = debt interest saved over the CD's remaining term − penalty − CD interest given up
   - recommend breaking the CD only if net benefit > 0; always show all three numbers.
7. **Snowball alternative.** If there are 3 or more debts under $2,000, also compute snowball
   (smallest balance first) and report `snowball_extra_cost` (extra interest vs avalanche). It is a
   valid choice for motivation; state the cost.
8. **Flags:** `no_debt`, `no_surplus` (surplus ≤ 0: no extra monthly payment possible),
   `tradeoff_low_cushion`, `keep_cash_earns_more`, `promo_rate_expiring` (if present),
   `prepayment_penalty_assumed_none`.

### How the waterfall and payoff rules fit together
- The **waterfall governs monthly surplus**. **Payoff rule 1 governs lump sums from existing cash.**
- So monthly surplus may go to a high-APR card (step 3) before the cushion is full (step 4), but a
  lump sum never drains the cushion below 3 months (1 month for cards ≥ 20% APR, flagged).

---

## 3. Candidate moves — `candidate_moves(profile)`

The only moves the advisor may recommend. Each is generated by code when its condition holds.

| move_code | Condition |
|-----------|-----------|
| `build_starter_cushion` | waterfall step 1 not done |
| `confirm_401k_match` | waterfall step 2 is `unknown` |
| `pay_card_high_apr` | any card with APR ≥ 8% and balance > 0 |
| `pay_loan_high_apr` | any installment loan with APR ≥ 8% and balance > 0 |
| `build_full_cushion` | waterfall step 4 not done |
| `increase_retirement` | waterfall step 5 not done and not retired |
| `start_education_savings` | waterfall step 6 applies and is not done |
| `start_credit_building` | credit score < 620 and no open card |
| `move_idle_cash_to_hys` | liquid cash > 6 months of essentials, or checking > 2 months of essentials; suppressed while step 3 is open and `safe_lump_sum` > 0 (that cash goes to the debt) |
| `cd_renewal_review` | a CD matures within 6 months |
| `use_extra_paycheck` | a three-paycheck month within the next 90 days |
| `recently_freed_payment` | a debt closed within the last 90 days, or a loan ends within 90 days |
| `automate_savings` | surplus > 0 and regular savings transfers cover less than `automate_coverage_min` (50%) of the surplus |
| `close_monthly_gap` | surplus < 0 (spending exceeds income); listed first |

Each candidate carries the facts its explanation needs (as placeholders) and a priority hint from
the waterfall position.

---

## 4. How the advisor uses these

- **Inputs:** waterfall (current step, gaps, what is done), payoff priority when relevant, candidate
  moves with their facts, forecast facts and labels, stress test result. Everything as facts and
  placeholders; no raw codes or numbers in the model's view.
- **Output:**
  ```
  { verdict: "on_track" | "plan_ahead" | "act_now",
    next_moves: [{ move_code, title_text, why_text }]   # max 3
  }
  ```
- **`verdict` is set by code**, from the waterfall's current step and `overall_status`:
  - `act_now`: current step is 1 or 3, or `overall_status = at_risk`
  - `plan_ahead`: current step is 4–6, or `overall_status = watch`
  - `on_track`: otherwise
- **Negative surplus** (`close_monthly_gap` fires) sets the floor at `plan_ahead`, and `act_now` when the
  chance of overdraft is above 0 or the customer's investments cover fewer than
  `retiree_investment_cover_months` of essentials. In that case the waterfall's `current_step` is `"gap"`
  (`first_open_step` keeps the step it would have been). Implemented as `verdict()` in `formulas/rules.py`.
- **The model chooses up to 3 `move_code`s from the candidate list, orders them, and writes
  `title_text` and `why_text` using placeholders.** An unknown move code is rejected and retried.
- Grounding (figures), wording, judgment-free and style checks all apply; retry once, then the
  deterministic template (top 3 candidates in waterfall order, template text).
- The advisor states the "retirement before education" rule plainly when both apply.

---

## 5. Expected results — golden personas (acceptance tests)

| Persona | Current waterfall step | Candidate moves that must be present |
|---------|------------------------|--------------------------------------|
| C002 | 4 (full cushion) | `build_full_cushion`, `start_credit_building`, `recently_freed_payment` |
| C067 | 3 (high-APR card) | `pay_card_high_apr`, `automate_savings` (if surplus > 0) |
| C095 | 5 or 7 | `move_idle_cash_to_hys`, `cd_renewal_review`, `increase_retirement` |
| C010 | retiree (steps 2, 5 `not_applicable`) | no `act_now`; `cd_renewal_review` / `move_idle_cash_to_hys` only if their conditions hold |
| C059 | per its data | `recently_freed_payment` (loan ending), plus its waterfall step's move |

Tests assert the current step and that the listed move codes are present (other valid candidates
may also appear).

---

## 6. Thresholds (all in `assumptions.json`)

| Key | Value | Source / rationale |
|-----|-------|--------------------|
| `starter_cushion` | $1,000 | common personal-finance guideline for a starter emergency fund |
| `high_apr_threshold` | 8% | design choice: above a typical long-run expected investment return, paying debt is the better guaranteed return |
| `cushion_months` | 3 | widely used planner guideline (3–6 months of essentials) |
| `cushion_months_high` | 6 | upper end of the same guideline, for less stable situations |
| `retirement_target_pct` | 15% of gross | common retirement guideline including employer match (e.g., Fidelity's 15% guideline) |
| `card_low_cushion_apr` | 20% | design choice: card interest at this level outweighs the risk of a thinner cushion, with a flag |
| `low_cushion_months` | 1 | floor for the card exception |
| `mortgage_extra_rate` | 6.5% | design choice: only rates above this compete with safe returns and investing |
| `safe_savings_rate` | from savings yield assumption | high-yield savings rate used across the app |
| `snowball_small_balance` | $2,000 | design choice for "several small debts" |
| `snowball_min_count` | 3 | design choice |
| `idle_cash_months` | 6 (total liquid) / 2 (checking) | design choice for "more cash than needed" |
| `cd_review_months` | 6 | design choice |
| `automate_coverage_min` | 50% | design choice: automate savings when regular transfers cover less than this share of the surplus |
| `retiree_investment_cover_months` | 12 | design choice: a retiree whose investments cover this many months of essentials has a drawdown backstop for step 4 |
| `credit_building_score` | 620 | approximate subprime cutoff used by many lenders |

Change a threshold here and in `assumptions.json` together; the tests read the values from
`assumptions.json`.
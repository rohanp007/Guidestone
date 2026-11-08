#!/usr/bin/env python3
"""
Checks that derive.py's output ties back to the ledger, for every customer. Exits non-zero on any failure.

  1. double-counting   active recurring expenses + variable_spend  ==  ledger monthly_non_debt_expenses  (within 5%)
  2. liquid cash flow  (a) transactions sum to the change in checking+savings balances (exact)
                       (b) monthly change in liquid cash  ==  recurring (excl. savings transfers, which are
                           liquid-neutral) - variable_spend_checking + known one-off movements (CD funding, outside
                           top-ups / sweeps, interest, ended loans)  (within 3% of net income)
  3. history           last month == the loader's Summary (liquid, assets, debt, net worth); excluding home /
                       vehicle estimates it equals the ledger net worth; 24 months, oldest first;
                       income / expenses average to net_monthly / monthly_non_debt_expenses
  4. spending profile  seasonality factors average to 1.0 (+-0.02) per category; samples x seasonal index reproduce the
                       actual monthly checking variable spend; subscription_total matches the ledger's subscription
                       category (checking + card); overdraft_topups == customers.overdraft_plug_deposits;
                       December shopping seasonality, across customers, is 1.10-1.20 (the generator uses a raw 1.35,
                       shrunk halfway to 1.0 this should land near 1.15)
  5. step 1-3 inputs   monthly_total_samples x the total seasonal index reproduce the actual total variable spend (checking + card);
                       bills that vary (utilities) carry residuals that average ~0, fixed ones (rent, loans, subscriptions) carry none
  6. practical filters notable_trends are >= 10%/yr AND >= $25/mo and a subset of the statistical trends; categories
                       under $20/mo have flat seasonality; the paycheck-to-paycheck insight flags every customer who
                       needed top-ups and never flags on soft signals alone

Run:  .venv/bin/python3 scripts/check_derive.py          (from the repo root)
"""
import os, statistics, sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.data import derive                                                  # noqa: E402
from app.data.loader import load_profiles, transactions_for                  # noqa: E402
from app.formulas.health import paycheck_to_paycheck                         # noqa: E402

EXPENSE_KINDS = {"housing", "utility", "insurance", "subscription"}
TOL_EXPENSES, TOL_LIQUID, CENT = 0.05, 0.03, 0.011
TOL_SEASON_MEAN, TOL_SUBS = 0.02, 0.05
DEC_SHOPPING_RANGE = (1.10, 1.20)


def main():
    profiles = load_profiles()
    failures, worst = [], {"expenses": 0.0, "liquid": 0.0}
    dec_shopping, flagged = [], []

    for cid, p in profiles.items():
        n = len(p.history)

        # 1. double-counting
        rec_exp = -sum(r.monthly_amount for r in p.recurring if r.kind in EXPENSE_KINDS)
        gap = abs(rec_exp + sum(p.variable_spend.values()) - p.monthly_non_debt_expenses) / p.monthly_non_debt_expenses
        worst["expenses"] = max(worst["expenses"], gap)
        if gap > TOL_EXPENSES:
            failures.append(f"{cid}: recurring expenses + variable_spend is {gap:.1%} off ledger expenses")
        if sum(p.variable_spend_checking.values()) > sum(p.variable_spend.values()) + CENT:
            failures.append(f"{cid}: variable_spend_checking exceeds variable_spend")

        # 2. liquid cash flow
        tx = derive._tag(transactions_for(cid))
        _, in_recurring = derive._recurring(tx, p.as_of_date)
        liquid_rows = tx[tx["account_type"].isin(["checking", "savings"])]
        opening = sum(a.opening_balance for a in p.accounts if a.type in ("checking", "savings"))
        delta = p.summary.liquid_cash - opening
        if abs(delta - liquid_rows["balance_change"].sum()) > CENT:
            failures.append(f"{cid}: liquid balance change {delta:.2f} != sum of transactions")
        neutral = liquid_rows["category"].isin(["savings_contribution", "savings_withdrawal"])
        rec_rows = in_recurring[liquid_rows.index] & ~neutral
        var_rows = (liquid_rows["account_type"] == "checking") & (liquid_rows["flow_class"] == "expense") & ~in_recurring[liquid_rows.index]
        one_offs = liquid_rows[~neutral & ~rec_rows & ~var_rows]["balance_change"].sum() / n
        forecast = (sum(r.monthly_amount for r in p.recurring if r.kind != "savings_transfer")
                    - sum(p.variable_spend_checking.values()) + one_offs)
        gap = abs(delta / n - forecast) / p.net_monthly
        worst["liquid"] = max(worst["liquid"], gap)
        if gap > TOL_LIQUID:
            failures.append(f"{cid}: monthly liquid change {delta / n:.0f} vs forecast {forecast:.0f} ({gap:.1%} of net income)")

        # 3. history
        last, h = p.history[-1], p.history
        est = sum(a.balance for a in p.accounts if a.is_estimated)
        ledger_nw = sum(a.balance for a in p.accounts if not a.is_liability and not a.is_estimated) - p.summary.total_debt
        checks = {"liquid": (last.liquid, p.summary.liquid_cash), "total_assets": (last.total_assets, p.summary.total_assets),
                  "total_debt": (last.total_debt, p.summary.total_debt), "net_worth": (last.net_worth, p.summary.net_worth),
                  "net_worth ex-estimates": (last.net_worth - est, ledger_nw),
                  "avg income": (sum(m.income for m in h) / n, p.net_monthly),
                  "avg expenses": (sum(m.expenses for m in h) / n, p.monthly_non_debt_expenses)}
        for name, (got, want) in checks.items():
            if abs(got - want) > CENT:
                failures.append(f"{cid}: history {name} {got:.2f} != {want:.2f}")
        if n != 24 or [m.month for m in h] != sorted(m.month for m in h):
            failures.append(f"{cid}: history has {n} months or is not oldest first")

        # 4. spending profile
        sp = p.spending
        if sp is None:
            failures.append(f"{cid}: no spending profile")
            continue
        for cat, f in sp.seasonality.items():
            if abs(sum(f.values()) / 12 - 1) > TOL_SEASON_MEAN:
                failures.append(f"{cid}: {cat} seasonality averages {sum(f.values()) / 12:.3f}")
        months = pd.PeriodIndex([m.month for m in h], freq="M")
        actual = (tx[(tx["account_type"] == "checking") & (tx["flow_class"] == "expense") & ~in_recurring]
                  .groupby("month")["amount"].sum().reindex(months, fill_value=0.0))
        index = derive.aggregate_seasonality(p.variable_spend_checking, sp.seasonality)
        for period, sample in zip(months, sp.monthly_variable_samples):
            if abs(sample * index[period.month] - actual[period]) > CENT:
                failures.append(f"{cid}: {period} sample x seasonality {sample * index[period.month]:.2f} != actual {actual[period]:.2f}")
                break
        if len(sp.monthly_variable_samples) != n:
            failures.append(f"{cid}: {len(sp.monthly_variable_samples)} variable samples, expected {n}")
        total_actual = (tx[(tx["flow_class"] == "expense") & ~in_recurring].groupby("month")["amount"].sum().reindex(months, fill_value=0.0))
        total_index = derive.aggregate_seasonality(p.variable_spend, sp.seasonality)
        for period, sample in zip(months, sp.monthly_total_samples):
            if abs(sample * total_index[period.month] - total_actual[period]) > CENT:
                failures.append(f"{cid}: total sample x seasonality {sample * total_index[period.month]:.2f} != actual {total_actual[period]:.2f} in {period}")
                break
        for r in p.recurring:
            if r.residuals:
                paid = [x for x in r.residuals if x != 0]
                if r.kind not in derive.NOISY_KINDS or abs(statistics.mean(paid)) > 0.02 or len(r.residuals) != len(sp.monthly_variable_samples):
                    failures.append(f"{cid}: {r.description} has malformed residuals")
            elif r.kind == "utility":
                failures.append(f"{cid}: {r.description} varies by month but has no residuals")
        ledger_subs = tx[(tx["category"] == "subscriptions") & (tx["flow_class"] == "expense")]["amount"].sum() / n
        if abs(sp.subscription_total - ledger_subs) > max(2.0, TOL_SUBS * ledger_subs):
            failures.append(f"{cid}: subscription_total {sp.subscription_total:.2f} vs ledger {ledger_subs:.2f}")
        if sp.overdraft_topups != p.overdraft_plug_deposits:
            failures.append(f"{cid}: overdraft_topups {sp.overdraft_topups} != overdraft_plug_deposits {p.overdraft_plug_deposits}")
        if len(sp.top_categories) > 5 or sum(c.share for c in sp.top_categories) > 1 + 1e-9:
            failures.append(f"{cid}: top_categories malformed")
        for t in sp.notable_trends:
            if abs(t.annual_change) < derive.NOTABLE_TREND_PCT or abs(t.monthly_change) < derive.NOTABLE_TREND_DOLLARS or sp.trend.get(t.category, 0) == 0:
                failures.append(f"{cid}: {t.category} trend {t.annual_change:+.1%} / {t.monthly_change:+.0f}$ should not be notable")
        for cat, f in sp.seasonality.items():
            if p.variable_spend.get(cat, 0) < derive.SEASON_MIN_MONTHLY and any(abs(v - 1) > 1e-12 for v in f.values()):
                failures.append(f"{cid}: {cat} averages ${p.variable_spend.get(cat, 0):.0f}/mo but has seasonality")
        for cat, byc in sp.seasonality_se.items():
            if any(v < 0 for v in byc.values()) or (p.variable_spend.get(cat, 0) < derive.SEASON_MIN_MONTHLY and any(v != 0 for v in byc.values())):
                failures.append(f"{cid}: {cat} seasonality_se is malformed")
        outs = tx[(tx["account_type"] == "checking") & (tx["category"] == "external_cash_management") & (tx["balance_change"] < 0)]
        if (len(outs) >= derive.SWEEP_MIN_EVENTS) != (sp.sweep_rule is not None):
            failures.append(f"{cid}: {len(outs)} sweeps in the ledger but sweep_rule is {sp.sweep_rule}")
        if sp.sweep_rule and not (sp.sweep_rule.target < sp.sweep_rule.threshold and 1 <= sp.sweep_rule.day <= 31):
            failures.append(f"{cid}: sweep rule {sp.sweep_rule} is malformed")
        verdict = paycheck_to_paycheck(p)
        near_zero = sp.buffer_ratio_min < 0.02
        if sp.overdraft_topups >= 1 and not verdict.flag:
            failures.append(f"{cid}: needed top-ups but not flagged paycheck-to-paycheck")
        if verdict.flag and not (near_zero or sp.overdraft_topups >= 1):
            failures.append(f"{cid}: flagged paycheck-to-paycheck on soft signals alone")
        flagged.append(verdict.flag)
        if "shopping" in sp.seasonality:
            dec_shopping.append(sp.seasonality["shopping"][12])

    print(f"paycheck-to-paycheck flagged: {sum(flagged)}/{len(flagged)} | notable trends: "
          f"{sum(len(p.spending.notable_trends) for p in profiles.values())}")
    dec = statistics.median(dec_shopping)
    print(f"December shopping seasonality: median {dec:.3f} across {len(dec_shopping)} customers (expect {DEC_SHOPPING_RANGE[0]}-{DEC_SHOPPING_RANGE[1]})")
    if not DEC_SHOPPING_RANGE[0] <= dec <= DEC_SHOPPING_RANGE[1]:
        failures.append(f"December shopping seasonality median {dec:.3f} outside {DEC_SHOPPING_RANGE}")

    print(f"{len(profiles)} customers checked | worst gap: expenses {worst['expenses']:.2%} (limit {TOL_EXPENSES:.0%}), "
          f"liquid {worst['liquid']:.2%} of net income (limit {TOL_LIQUID:.0%})")
    if failures:
        print(f"{len(failures)} FAILURE(S):\n  " + "\n  ".join(failures[:25]))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()

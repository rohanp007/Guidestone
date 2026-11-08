#!/usr/bin/env python3
"""
Backtest calibration, step by step. Re-runs the 18-month-train / 6-month-test backtest for every customer with the model's
variance sources switched on cumulatively, and reports band coverage and error by group.

  baseline   checking-only spend, bills fixed
  + step 1   total spend (checking + card), card paid through a simulated card balance
  + step 2   parameter uncertainty and a small drift in the spending level
  + step 3   noise in bills whose amount really varies (utilities), bootstrapped from their own history
  + step 2b  uncertainty in the seasonal factors themselves, wider for calendar months seen once than twice
  + sweeps   customers who move surplus out of checking do so by a rule learned from their own sweeps

Main metric: coverage at month-end days (beyond 90 days the chart is monthly). Also shown: every day, the share of customers
under 0.5, and the stricter error (larger of mean daily error and cumulative-net-change error, as a share of a month's outflow).
Customers who funded a CD during the test window are left out: nobody can forecast that, so a "miss" would be misleading.

Run:  .venv/bin/python3 scripts/measure_coverage.py          (from the repo root)
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import numpy as np                                                            # noqa: E402
import pandas as pd                                                           # noqa: E402

from app.data.loader import load_profiles, transactions_for                  # noqa: E402
from app.formulas import forecast as fc                                      # noqa: E402

STEPS = [("baseline", dict(total_spend=False, uncertainty=False, recurring_noise=False, seasonal_uncertainty=False, sweeps=False)),
         ("+ step 1: total spend + card", dict(total_spend=True, uncertainty=False, recurring_noise=False, seasonal_uncertainty=False, sweeps=False)),
         ("+ step 2: uncertainty + drift", dict(total_spend=True, uncertainty=True, recurring_noise=False, seasonal_uncertainty=False, sweeps=False)),
         ("+ step 3: recurring noise", dict(total_spend=True, uncertainty=True, recurring_noise=True, seasonal_uncertainty=False, sweeps=False)),
         ("+ step 2b: seasonal uncertainty", dict(total_spend=True, uncertainty=True, recurring_noise=True, seasonal_uncertainty=True, sweeps=False)),
         ("+ sweep rule", dict(total_spend=True, uncertainty=True, recurring_noise=True, seasonal_uncertainty=True, sweeps=True))]


def funded_cd_in_window(profile, cutoff):
    tx = transactions_for(profile.id)
    return bool(((tx["posted_date"] > pd.Timestamp(cutoff)) & (tx["description"] == "Transfer to CD")).any())


def main():
    profiles = load_profiles()
    has_card = {cid: bool(p.accounts_of("credit_card")) for cid, p in profiles.items()}
    saved = dict(fc.OPTIONS)
    results = {}
    try:
        for label, opts in STEPS:
            fc.OPTIONS.update(opts)
            results[label] = {cid: fc.run_backtest(p) for cid, p in profiles.items()}
    finally:
        fc.OPTIONS.update(saved)

    skip = {cid for cid, p in profiles.items() if funded_cd_in_window(p, results[STEPS[0][0]][cid].cutoff)}
    print(f"left out (funded a CD in the test window, unforecastable): {sorted(skip)}\n")
    print(f"{'':<32}{'group':<9}{'n':>4}{'month-end':>11}{'all days':>10}{'<0.5 m-end':>12}{'<0.5 days':>11}{'err 90d':>9}{'err 180d':>10}")
    for label, _ in STEPS:
        for group, sel in [("all", lambda c: True), ("card", lambda c: has_card[c]), ("no card", lambda c: not has_card[c])]:
            bt = [b for c, b in results[label].items() if c not in skip and sel(c) and b]
            me = np.array([b.band_coverage_month_end for b in bt]); dy = np.array([b.band_coverage for b in bt])
            err = lambda h: np.median([max(getattr(b, f"outflow_err_{h}d"), getattr(b, f"net_change_err_{h}d")) for b in bt])
            print(f"{label if group == 'all' else '':<32}{group:<9}{len(bt):>4}{me.mean():>11.2f}{dy.mean():>10.2f}{(me < .5).mean():>11.0%} {(dy < .5).mean():>10.0%} {err(90):>8.1%} {err(180):>9.1%}")
        print()


if __name__ == "__main__":
    main()

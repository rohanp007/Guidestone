#!/usr/bin/env python3
"""
Checks for formulas/forecast.py. Exits non-zero on any failure.

  1. seed          the same arguments give identical output
  2. speed         a 24-month, 1,000-path forecast takes < 100 ms (median over all customers; backtest already cached)
  3. history tie   with all escalation switched off, the engine reproduces history for customers WITHOUT a revolving card: the median gap
                   to "history minus one-off cash movements" is < 1% of income. Revolvers are excluded on purpose: their card payment is
                   simulated from the balance, so it falls as the balance is paid down, while history averaged the higher early payments.
                   C001 (a revolver) is reported, not asserted; the literal check (escalation ON vs history) cannot pass either.
  4. ending loan   a loan that ends inside the horizon has a loan_ends event and the p50 steps up by the payment. Measured by
                   running the forecast with and without that loan on the same seed: the p50 gap is then exactly the loan, whereas raw
                   month-over-month p50 swings by thousands when a card payment slides over a weekend (Feb 28 -> Mar 1).
  5. risk          paycheck-to-paycheck customers hit cushion_risk (buffer = one month of essentials, the default indicator) far more
                   often than the rest; overdraft_risk (buffer $0, the urgent alert) is reported alongside
  6. backtest      customers who opened a CD in the test window are left out (unforecastable). The model's raw bands are over-confident,
                   so they are calibrated (split-conformal, per horizon, scripts/fit_calibration.py). Passing means: fit on one half of
                   the customers, the calibrated band contains the actual outcome 72-88% of the time on the OTHER half, at month-end (the
                   main metric) and on every day; and the saved calibration.json is current (refitting gives the same factors, +-10%).
                   Raw coverage and the errors are reported.
  7. business days no scheduled paycheck or bill falls on a weekend
  8. risk reliability  overdraft_risk / cushion_risk, computed on calibrated paths, scored on HELD-OUT customers (each scored with factors fit on
                   the other half) against what happened in the 6 test months. Pass: mean forecast within 0.05 of the observed rate; Brier
                   score no worse than the raw paths'; customers forecast under 10% rarely have the event (<= 5%); those forecast at 60%+ usually do (>= 80%).

Run:  .venv/bin/python3 scripts/check_forecast.py          (from the repo root)
"""
import os, statistics, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import numpy as np                                                            # noqa: E402

from app.data import derive                                                  # noqa: E402
from app.data.loader import transactions_for                                 # noqa: E402

from app.data.loader import load_profiles                                    # noqa: E402
from app.formulas import forecast as fc                                      # noqa: E402
from app.formulas.health import paycheck_to_paycheck                         # noqa: E402

MAX_MS, COVERAGE_RANGE, C001_TOL, C001_ABS_SHARE, LOAN_STEP_TOL, SHORTFALL_GAP, MEDIAN_TIE = 100, (0.72, 0.88), 0.10, 0.01, 0.05, 0.25, 0.01


def main():
    profiles = load_profiles()
    failures = []
    t0 = time.time()
    backtests = {cid: fc.backtest_for(p) for cid, p in profiles.items()}
    print(f"backtests ready for {len(backtests)} customers ({time.time() - t0:.1f}s; cached after the first run)")

    # 1. seed
    p = profiles["C001"]
    a, b = fc.forecast_liquid(p).model_dump(), fc.forecast_liquid(p).model_dump()
    if a != b: failures.append("same seed gave different output")
    if fc.forecast_liquid(p, seed=1).model_dump() == a: failures.append("a different seed gave identical output")

    # 2. speed
    times, forecasts = [], {}
    for cid, p in profiles.items():
        t = time.perf_counter()
        forecasts[cid] = fc.forecast_liquid(p)
        times.append(1000 * (time.perf_counter() - t))
    med = statistics.median(times)
    print(f"speed: median {med:.0f} ms, max {max(times):.0f} ms (24 months, {forecasts['C001'].paths} paths)")
    if med >= MAX_MS: failures.append(f"median forecast time {med:.0f} ms >= {MAX_MS} ms")

    # 3. history tie
    p = profiles["C001"]
    opening = sum(x.opening_balance for x in p.accounts if x.is_liquid)
    hist = (p.summary.liquid_cash - opening) / 24
    got = forecasts["C001"].monthly_net
    print(f"C001 literal check: historical {hist:+.2f}/mo vs forecast with escalation {got.mean:+.2f}/mo (p10 {got.p10:+.2f}, p90 {got.p90:+.2f}) -- not comparable, see above")
    real_assumptions = fc.load_assumptions
    flat = {k: ({**v, "value": 0.0} if k != "forecast_paths" else v) for k, v in real_assumptions().items()}
    fc.load_assumptions = lambda: flat
    try:
        flat_forecast = {cid: fc.forecast_liquid(p, with_backtest=False).monthly_net.mean for cid, p in profiles.items()}
    finally:
        fc.load_assumptions = real_assumptions
    c001 = flat_forecast["C001"]
    print(f"C001 (a revolver) with escalation off: forecast {c001:+.2f}/mo vs history {hist:+.2f}/mo -- its card payment falls as the balance is paid down")
    gaps = []
    for cid, p in profiles.items():
        if p.spending.carries_card_balance or p.spending.sweep_rule: continue     # sweepers: history treats the sweeps as one-offs, the forecast models them
        tx = derive._tag(transactions_for(cid))
        _, in_recurring = derive._recurring(tx, p.as_of_date)
        liquid_rows = tx[tx["account_type"].isin(["checking", "savings"])]
        neutral = liquid_rows["category"].isin(["savings_contribution", "savings_withdrawal"])
        rec_rows = in_recurring[liquid_rows.index] & ~neutral
        var_rows = (liquid_rows["account_type"] == "checking") & (liquid_rows["flow_class"] == "expense") & ~in_recurring[liquid_rows.index]
        one_offs = liquid_rows[~neutral & ~rec_rows & ~var_rows]["balance_change"].sum() / 24
        hist_p = (p.summary.liquid_cash - sum(x.opening_balance for x in p.accounts if x.is_liquid)) / 24
        card = next((x for x in p.accounts if x.type == "credit_card"), None)
        retired_balance = (card.opening_balance - card.balance) / 24 if card else 0.0     # cash that paid off the opening balance: not repeated
        gaps.append(abs(flat_forecast[cid] - (hist_p - one_offs + retired_balance)) / p.net_monthly)
    print(f"engine (no escalation) vs history minus one-offs, {len(gaps)} customers without a revolving card or a sweep rule: median gap {statistics.median(gaps):.2%} "
          f"of income, p90 {np.percentile(gaps, 90):.2%} (the tail is loans that end inside the horizon, which the forecast correctly drops)")
    if statistics.median(gaps) > MEDIAN_TIE: failures.append(f"median engine-vs-history gap {statistics.median(gaps):.2%} > {MEDIAN_TIE:.0%}")

    # 4. ending loan: same seed with and without the loan; the p50 gap is exactly the cumulative payments
    checked = 0
    for cid, f in forecasts.items():
        for e in (e for e in f.events if e.kind == "loan_ends"):
            p = profiles[cid]
            without = fc.forecast_liquid(p.model_copy(update={"recurring": [r for r in p.recurring if r.description != e.description.removesuffix(" ends")]}),
                                         with_backtest=False)
            gap = np.array([0.0] + [w.p50 - m.p50 for w, m in zip(without.monthly, f.monthly)])
            slope = np.diff(gap)                                # cash the loan took in each month
            j = next(i for i, m in enumerate(f.monthly) if (m.day.year, m.day.month) == (e.day.year, e.day.month))
            if j + 1 >= len(slope): continue
            checked += 1
            before, after = slope[max(0, j - 2):j + 1].mean(), slope[j + 1:j + 4].mean()
            if abs((before - after) - e.monthly_impact) > LOAN_STEP_TOL * e.monthly_impact or abs(after) > LOAN_STEP_TOL * e.monthly_impact:   # a small residual remains: the freed cash earns savings interest
                failures.append(f"{cid}: {e.description}: p50 gap steps {before:.0f} -> {after:.0f}, payment is {e.monthly_impact:.0f}")
    print(f"ending loans checked: {checked}")
    if checked == 0: failures.append("no ending loan inside the horizon to check")

    # 5. risk outputs, from the default-argument forecasts above
    flag = {cid: paycheck_to_paycheck(p).flag for cid, p in profiles.items()}
    for name in ("overdraft_risk", "cushion_risk"):
        prob = {cid: getattr(f, name).probability for cid, f in forecasts.items()}
        yes, no = [prob[c] for c in prob if flag[c]], [prob[c] for c in prob if not flag[c]]
        print(f"{name}: paycheck-to-paycheck (n={len(yes)}) mean {statistics.mean(yes):.2f}; everyone else mean {statistics.mean(no):.2f}"
              f" | customers with any risk: {sum(v > 0 for v in prob.values())}/100")
        if name == "cushion_risk" and statistics.mean(yes) < statistics.mean(no) + SHORTFALL_GAP:
            failures.append(f"flagged {name} {statistics.mean(yes):.2f} is not {SHORTFALL_GAP} above the others' {statistics.mean(no):.2f}")
        if name == "overdraft_risk" and statistics.mean(yes) <= statistics.mean(no):
            failures.append(f"flagged {name} {statistics.mean(yes):.2f} is not above the others' {statistics.mean(no):.2f}")

    # 6. backtest
    curves = {cid: fc.backtest_curves(p) for cid, p in profiles.items()}
    kept = [c for c in curves.values() if c is not None and c.known_one_off is None]
    left_out = sorted(cid for cid, c in curves.items() if c is not None and c.known_one_off)
    bt = [b for cid, b in backtests.items() if b and cid not in left_out]
    print(f"backtest: {len(bt)} customers x {bt[0].days} days; left out (unforecastable): {left_out}")
    held = fc.split_half_check(kept)
    print(f"  coverage of the p10-p90 band (target 0.80): raw month-end {held['uncalibrated_month_end']:.2f} / every day {held['uncalibrated_all_days']:.2f}"
          f" -> calibrated, held-out customers: month-end {held['held_out_month_end']:.2f} / every day {held['held_out_all_days']:.2f}")
    for label, v in (("month-end", held["held_out_month_end"]), ("every-day", held["held_out_all_days"])):
        if not COVERAGE_RANGE[0] <= v <= COVERAGE_RANGE[1]:
            failures.append(f"held-out calibrated {label} coverage {v:.2f} outside {COVERAGE_RANGE}")
    saved = fc._calibration()
    if saved is None:
        failures.append("backend/app/calibration.json is missing: run scripts/fit_calibration.py")
    else:
        for key, fresh in (("daily_factors", fc.fit_calibration(kept)), ("month_end_factors", fc.fit_calibration(kept, month_end_only=True))):
            if any(abs(a - b) > 0.10 * b for a, b in zip(saved[key], fresh)):
                failures.append(f"calibration.json {key} {np.round(saved[key], 2).tolist()} is stale; a refit gives {np.round(fresh, 2).tolist()}: re-run scripts/fit_calibration.py")
    mape = {h: statistics.median(getattr(b, f"mape_{h}d") for b in bt if getattr(b, f"mape_{h}d") is not None) for h in (30, 90, 180)}
    print(f"  lenient MAPE (relative to the balance, which sits near zero for many customers): 30d {mape[30]:.1%}, 90d {mape[90]:.1%}, 180d {mape[180]:.1%}")
    for h in (30, 90, 180):
        mean_err = statistics.median(getattr(b, f"outflow_err_{h}d") for b in bt)
        net_err = statistics.median(getattr(b, f"net_change_err_{h}d") for b in bt)
        strict = [max(getattr(b, f"outflow_err_{h}d"), getattr(b, f"net_change_err_{h}d")) for b in bt]      # each customer's stricter error, then the typical customer
        print(f"  {h:>3}d, error as share of a month's outflow: mean daily {mean_err:.1%} | cumulative net change {net_err:.1%} "
              f"-> quote the stricter, per customer: median {statistics.median(strict):.1%}, p75 {np.percentile(strict, 75):.1%}, p90 {np.percentile(strict, 90):.1%}")

    # 7. business days
    bad = 0
    for cid, p in profiles.items():
        sch = fc.build_schedule(p, 24, {k: v["value"] for k, v in fc.load_assumptions().items()})
        weekend = [e for e in sch.cash if e[0].weekday() >= 5]
        if weekend:
            bad += 1
            failures.append(f"{cid}: {len(weekend)} scheduled cash events on a weekend, e.g. {weekend[0][3]} on {weekend[0][0]}")
    print(f"business days: {len(profiles) - bad}/{len(profiles)} schedules have no weekend cash events")

    # 8. risk reliability on held-out customers
    ids = [cid for cid, c in curves.items() if c is not None and c.known_one_off is None]
    half_a, half_b = ids[0::2], ids[1::2]
    fit = lambda cs: fc.fit_calibration([curves[c] for c in cs], month_end_only=True)
    from_other_half = {**{c: fit(half_b) for c in half_a}, **{c: fit(half_a) for c in half_b}}
    outcomes = {cid: (fc.backtest_risks(profiles[cid], from_other_half[cid]), fc.backtest_risks(profiles[cid], None)) for cid in ids}
    bins = [("P = 0", lambda x: x == 0), ("0-10%", lambda x: 0 < x <= .10), ("10-30%", lambda x: .10 < x <= .30),
            ("30-60%", lambda x: .30 < x <= .60), ("60-100%", lambda x: x > .60)]
    for name, prob, happened, in_state in (
            ("overdraft_risk (needed outside money)", lambda r: r.p_overdraft, lambda r: r.overdraft_happened, lambda r: False),
            ("cushion_risk (fell below one month of essentials)", lambda r: r.p_cushion, lambda r: r.cushion_happened, lambda r: r.start_liquid < r.cushion)):
        use = {cid: o for cid, o in outcomes.items() if not in_state(o[0])}
        print(f"\n{name}: {len(use)} held-out customers ({len(outcomes) - len(use)} already in that state at the cutoff, left out)")
        print(f"  {'forecast probability':<22}{'calibrated: n':>14}{'mean P':>8}{'happened':>10}{'raw: n':>10}{'mean P':>8}{'happened':>10}")
        for label, inside in bins:
            cells = []
            for k in (0, 1):                                              # 0 = calibrated, 1 = raw
                sel = [o[k] for o in use.values() if inside(prob(o[k]))]
                cells.append((len(sel), statistics.mean(prob(r) for r in sel) if sel else float("nan"), sum(happened(r) for r in sel)))
            (n0, m0, h0), (n1, m1, h1) = cells
            print(f"  {label:<22}{n0:>14}{m0:>8.2f}{(f'{h0}/{n0}' if n0 else '-'):>10}{n1:>10}{m1:>8.2f}{(f'{h1}/{n1}' if n1 else '-'):>10}")
        stats = {}
        for k, lab in ((0, "calibrated"), (1, "raw")):
            pr = np.array([prob(o[k]) for o in use.values()]); y = np.array([happened(o[k]) for o in use.values()], float)
            stats[lab] = (pr.mean(), y.mean(), float(np.mean((pr - y) ** 2)))
            print(f"  {lab:<11} mean forecast {pr.mean():.3f} vs observed rate {y.mean():.3f} | Brier {stats[lab][2]:.4f}")
        cal = [o[0] for o in use.values()]
        low, high = [happened(r) for r in cal if prob(r) < .10], [happened(r) for r in cal if prob(r) >= .60]
        print(f"  events among customers forecast under 10%: {sum(low)}/{len(low)}; among those forecast at 60%+: {sum(high)}/{len(high)}")
        if abs(stats["calibrated"][0] - stats["calibrated"][1]) > 0.05:
            failures.append(f"{name}: mean forecast {stats['calibrated'][0]:.3f} vs observed {stats['calibrated'][1]:.3f}")
        if stats["calibrated"][2] > stats["raw"][2] + 1e-12:
            failures.append(f"{name}: calibrated Brier {stats['calibrated'][2]:.4f} is worse than raw {stats['raw'][2]:.4f}")
        if low and sum(low) / len(low) > 0.05: failures.append(f"{name}: {sum(low)}/{len(low)} customers forecast under 10% had the event")
        if high and sum(high) / len(high) < 0.80: failures.append(f"{name}: only {sum(high)}/{len(high)} customers forecast at 60%+ had the event")

    if failures:
        print(f"{len(failures)} FAILURE(S):\n  " + "\n  ".join(failures[:25]))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()

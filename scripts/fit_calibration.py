#!/usr/bin/env python3
"""
Fit the band-calibration factors (split-conformal, one factor per horizon) and write backend/app/calibration.json.

The model's p10-p90 bands are over-confident: the actual outcome falls inside them less than 80% of the time in the backtest.
For each horizon (30 / 90 / 180 days) this learns the factor that scales the band around the median so it contains the outcome 80%
of the time. To show that is not just fitting the answer, customers are split in two halves: fit on one half, measure coverage on
the other (both ways). The production factors are then fit on all customers. Customers who opened a CD in the test window are
left out: nobody could forecast that.

Re-run after any change to the model, the data or the assumptions.

Run:  .venv/bin/python3 scripts/fit_calibration.py          (from the repo root)
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.data.loader import load_profiles                                    # noqa: E402
from app.formulas import forecast as fc                                      # noqa: E402


def main():
    profiles = load_profiles()
    curves = {cid: fc.backtest_curves(p) for cid, p in profiles.items()}
    kept = [c for c in curves.values() if c is not None and c.known_one_off is None]
    left_out = sorted(cid for cid, c in curves.items() if c is not None and c.known_one_off)
    print(f"{len(kept)} customers fitted; left out (unforecastable): {left_out}")

    raw_me, raw_days = fc.coverage_with(kept, None, None)
    held = fc.split_half_check(kept)
    daily, month_end = fc.fit_calibration(kept), fc.fit_calibration(kept, month_end_only=True)
    in_me, in_days = fc.coverage_with(kept, daily, month_end)
    print(f"raw bands:                       month-end {raw_me:.2f} | every day {raw_days:.2f}")
    print("held-out (fit on one half, test on the other):")
    print(f"   raw                           month-end {held['uncalibrated_month_end']:.2f} | every day {held['uncalibrated_all_days']:.2f}")
    print(f"   calibrated                    month-end {held['held_out_month_end']:.2f} | every day {held['held_out_all_days']:.2f}   (target {fc.CAL_NOMINAL:.2f})")
    fmt = lambda fs: ", ".join(f"{h}d x{f:.2f}" for h, f in zip(fc.CAL_ANCHORS, fs))
    print(f"factors on all customers (in-sample: month-end {in_me:.2f}, every day {in_days:.2f}):\n   monthly chart (month-end days): {fmt(month_end)}\n   daily chart (every day):        {fmt(daily)}")

    note = (f"p10-p90 bands scaled using backtest errors across {len(kept)} customers (monthly chart x{month_end[0]:.1f} / x{month_end[1]:.1f} / x{month_end[2]:.1f} "
            f"at 30 / 90 / 180 days); on held-out customers the monthly band contained the actual outcome {held['held_out_month_end']:.0%} of the time "
            f"(was {held['uncalibrated_month_end']:.0%}), and the daily band {held['held_out_all_days']:.0%} of days (was {held['uncalibrated_all_days']:.0%})")
    with open(fc.CALIBRATION_PATH, "w") as f:
        json.dump(dict(nominal=fc.CAL_NOMINAL, anchors_days=list(fc.CAL_ANCHORS), daily_factors=daily, month_end_factors=month_end, customers=len(kept), left_out=left_out,
                       held_out=held, options=fc.OPTIONS, note=note), f, indent=2)
        f.write("\n")
    print(f"\nwrote {os.path.relpath(fc.CALIBRATION_PATH, ROOT)}")


if __name__ == "__main__":
    main()

"""Small, curated fact sheets for the writing agents. They need a handful of numbers, not the whole result: the full forecast carries
assumptions with source text, month-by-month probability lists, backtest stats and a calibration note (about 1,000 tokens that do
not help write a sentence, and invite the model to invent figures)."""
from __future__ import annotations

from ..api.serialize import to_plain
from ..data.models import LiquidForecast


TREND_FLAT_FRACTION = 0.02        # |monthly gain| under 2% of the cushion (one month of essentials) counts as flat
OVERDRAFT_NONE, OVERDRAFT_LIKELY = 0.01, 0.5      # chance of overdraft: under 1% none, 50% and up likely, between possible


def overall_status(cushion_status: str, trend: str, overdraft_status: str) -> str:
    """The judgment, decided in code from the other labels. The UI shows it as the badge; the model never states it.
    at_risk: below the cushion now, falling, or an overdraft is possible or likely.  watch: dips below the cushion, or flat.
    on_track: stays above the cushion and rising."""
    if cushion_status == "below_now" or trend == "falling" or overdraft_status != "none":
        return "at_risk"
    if cushion_status == "dips_temporarily" or trend == "flat":
        return "watch"
    return "on_track"


def status_labels(start: float, cushion: float, share_below: float, last_below, gain: float, p_overdraft: float) -> dict:
    """The qualitative facts, decided here so the model describes them instead of judging the numbers itself.

    cushion_status: below_now (the balance starts under the cushion) | dips_temporarily (starts above, the median path dips under it
    for a while, until `cushion_status_until`) | stays_above.  trend: rising | flat | falling.  overdraft_status: none | possible | likely.  overall_status: on_track | watch | at_risk (see overall_status)."""
    if start < cushion:
        cushion_status = "below_now"
    else:
        cushion_status = "dips_temporarily" if share_below > 0 else "stays_above"
    flat = TREND_FLAT_FRACTION * cushion
    out = {"cushion_status": cushion_status,
           "trend": "rising" if gain > flat else "falling" if gain < -flat else "flat",
           "overdraft_status": "none" if p_overdraft < OVERDRAFT_NONE else "likely" if p_overdraft >= OVERDRAFT_LIKELY else "possible"}
    out["overall_status"] = overall_status(out["cushion_status"], out["trend"], out["overdraft_status"])
    if cushion_status != "stays_above" and last_below is not None:
        out["cushion_status_until"] = last_below
    return out


def forecast_facts(f: LiquidForecast, n_events: int = 3) -> dict:
    """Everything a sentence about the outlook can use, and nothing else."""
    cushion = f.cushion_risk
    point = lambda m: None if m is None else {"date": m.day, "p10": m.p10, "p50": m.p50, "p90": m.p90}
    out = {
        "as_of": f.as_of_date,
        "start_balance": f.start_liquid,
        "horizon_months": f.horizon_months,
        "monthly_gain": f.monthly_net.mean,                                    # average monthly change in liquid cash (negative = loss)
        "end_balance": point(f.end_balance),                                    # month-end balance at the end of the horizon (p10 = bad luck)
        "balance_in_12_months": None if f.balance_12m is None else {"date": f.balance_12m.day, "p50": f.balance_12m.p50},
        "low_point": {"date": f.low_point.day, "amount": f.low_point.p50, "bad_luck_amount": f.low_point.p10},
        "cushion": cushion.buffer,                                             # one month of essential expenses
        "share_of_days_below_cushion": cushion.share_of_days_below,           # a share of days (0.03 = 3% of days), not a probability
        "chance_of_overdraft": f.overdraft_risk.probability,                    # 0 = no simulated path ever goes below $0
        **status_labels(f.start_liquid, cushion.buffer, cushion.share_of_days_below, cushion.last_date_below_p50, f.monthly_net.mean,
                        f.overdraft_risk.probability),
        "next_events": [{"date": e.day, "what": e.description, "monthly_impact": e.monthly_impact, "date_assumed": e.assumed_timing}
                        for e in sorted(f.events, key=lambda e: e.day)[:n_events]],
    }
    return to_plain({k: v for k, v in out.items() if v is not None})

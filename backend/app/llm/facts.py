"""Small, curated fact sheets for the writing agents. They need a handful of numbers, not the whole result: the full forecast carries
assumptions with source text, month-by-month probability lists, backtest stats and a calibration note (about 1,000 tokens that do
not help write a sentence, and invite the model to invent figures)."""
from __future__ import annotations

from datetime import date

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


# ---- what the model sees -------------------------------------------------------------------------------------------------------
# The full fact sheet (codes, numbers, overall_status) is for code, the grounding check and the UI. The model gets a plain-language
# view with no codes and no numbers: label phrases, and {placeholders} that the app fills in. It cannot leak a name it never saw,
# or mistype a figure it never wrote.

CUSHION_TEXT = {"below_now": "starts below your safety cushion", "dips_temporarily": "dips below your safety cushion for a while",
                "stays_above": "stays above your safety cushion"}
TREND_TEXT = {"rising": "rising", "flat": "roughly flat", "falling": "falling"}
OVERDRAFT_TEXT = {"none": "no overdraft is expected", "possible": "an overdraft is possible", "likely": "an overdraft is likely"}


def _usd(x: float) -> str:
    return f"${abs(x):,.0f}"


def _day(iso: str, year: bool = False) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}" + (f", {d.year}" if year else "")


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%" if x >= 0.01 or x == 0 else f"{x * 100:.1f}%"


def placeholders(facts: dict) -> dict[str, str]:
    """{name: text} for every amount, date and percentage the model may refer to. Built from the facts, so each fills in a real figure."""
    ph = {"start_balance": _usd(facts["start_balance"]), "monthly_gain": _usd(facts["monthly_gain"]),
          "lowest_point": _usd(facts["low_point"]["amount"]), "lowest_point_date": _day(facts["low_point"]["date"]),
          "cushion": _usd(facts["cushion"]), "days_below_cushion": _pct(facts.get("share_of_days_below_cushion", 0)),
          "chance_of_overdraft": _pct(facts.get("chance_of_overdraft", 0))}
    if facts.get("cushion_status_until"):
        ph["cushion_until"] = _day(facts["cushion_status_until"])
    if end := facts.get("end_balance"):
        ph["end_balance"], ph["end_date"] = _usd(end["p50"]), _day(end["date"], year=True)
    if twelve := facts.get("balance_in_12_months"):
        ph["balance_in_12_months"] = _usd(twelve["p50"])
    for i, e in enumerate(facts.get("next_events", []), 1):
        ph[f"event_{i}"] = f"{e['what']} ({_day(e['date'], year=True)})"
    return ph


_PH_MEANS = {"start_balance": "balance today", "monthly_gain": "average monthly change in cash", "lowest_point": "lowest expected balance",
             "lowest_point_date": "when that lowest balance comes", "cushion": "size of the safety cushion (one month of essentials)",
             "days_below_cushion": "percentage of days spent below the cushion", "chance_of_overdraft": "chance of overdrawing the account",
             "cushion_until": "last day below the cushion", "end_balance": "typical balance at the end of the period",
             "end_date": "the date of that end balance", "balance_in_12_months": "typical balance a year from now"}


def salient(facts: dict) -> list[str]:
    """The placeholders worth offering this customer. Offered all of them, a small model uses all of them; offering the ones that
    explain the situation keeps the sentence short. (Any placeholder that exists still fills in if the model writes it.)"""
    ph = placeholders(facts)
    names = ["monthly_gain"]
    if facts["cushion_status"] != "stays_above":
        names += ["lowest_point", "lowest_point_date", "cushion", "cushion_until", "days_below_cushion"]
    if facts["overdraft_status"] != "none":
        names.append("chance_of_overdraft")
    names += ["end_balance", "end_date"]
    return [n for n in names if n in ph]


def model_view(facts: dict) -> str:
    """The fact sheet as the model sees it: plain phrases and {placeholders}, no codes, no numbers, no overall judgment."""
    ph = placeholders(facts)
    until = " until {cushion_until}" if "cushion_until" in ph and facts["cushion_status"] != "stays_above" else ""
    lines = [
        f"Customer: {'retired' if facts.get('employment') == 'retired' else 'employed'}"
        f"{', often runs low before payday' if facts.get('paycheck_to_paycheck') == 'yes' else ''}.",
        f"Over the next {facts['horizon_months']} months their balance is {TREND_TEXT[facts['trend']]}; it "
        f"{CUSHION_TEXT[facts['cushion_status']]}{until}; {OVERDRAFT_TEXT[facts['overdraft_status']]}.",
        "Placeholders you may use (write them exactly, with the braces; the app fills in the figure):"]
    lines += [f"{{{k}}} = {_PH_MEANS.get(k, 'an upcoming change')}" for k in salient(facts)]
    return "\n".join(lines)

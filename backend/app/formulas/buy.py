"""Buying a car: three ways to pay (cash, finance, mix), a verdict, and a recommended plan. Cars only.

Reuses amortized_payment / amortize-style loan math (formulas.debt), and the paired-seed scenario simulation (engine.simulate) with
`purchase_now` (cash leaves at month 0) and `add_loan` (the loan, paid straight to the seller). The APR comes from the research pack by
credit score, in code: it is never sent to the Researcher. The price is the cited starting MSRP, before tax, destination and fees.

Verdict (for the recommended plan): affordable when the monthly payment is at most 10% of gross monthly income AND the cushion holds its
floor in the cash forecast; stretch when one of the two fails; not_now when both fail. "The cushion holds" means the bad-luck (10th
percentile) cash never falls below the cushion target by more than the materiality threshold used for the payoff warnings.
Recommended plan: cash when the loan costs more than savings earn and paying cash leaves the cushion intact; otherwise mix (the
down payment is the safe lump sum) when the loan costs more than savings earn, else finance.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..config import assumptions as load_assumptions
from ..data.models import Base, MonthEndPoint, Profile
from ..engine import simulate as sm
from ..research import pack
from . import forecast as fc
from .debt import amortized_payment
from .payoff import SimSummary
from .registry import register
from . import rules
from .rules import _liquid

PlanCode = Literal["cash", "finance", "mix"]
Verdict = Literal["affordable", "stretch", "not_now"]
NAMES = {"cash": "Pay cash", "finance": "Finance it all", "mix": "Put the safe amount down, finance the rest"}


class SourceRef(Base):
    """Where a figure came from: shown as title + date, with the url as a link."""
    source_title: str
    url: str
    fetched_at: str


class BuyWarning(Base):
    code: Literal["not_enough_cash", "overdraft_risk", "competes_with_high_apr_debt"]
    amount: float | None = None               # not_enough_cash: how much cash is missing; overdraft_risk: the lowest bad-luck cash; competes: the debt's APR
    detail: str = ""


class BuyPlan(Base):
    status: Literal["ok", "infeasible"] = "ok"    # infeasible: the plan needs more cash than there is; its cash metrics are null
    warnings: list[BuyWarning] = Field(default_factory=list)
    code: PlanCode
    name: str
    down_payment: float                       # cash paid at purchase
    loan_amount: float
    monthly_payment: float
    term_months: int
    total_interest: float
    total_cost: float                         # price + interest
    cash_after: float | None                  # liquid cash right after buying
    payment_share_of_gross: float             # monthly payment / gross monthly income
    payment_ok: bool
    cushion_floor: float
    cushion: SimSummary | None                # cash forecast with this purchase (daily-low basis)
    cushion_shortfall: float | None           # how far the bad-luck daily low gets under the floor (0 = never)
    cushion_holds: bool | None
    apr_source: SourceRef | None = None       # where the loan rate came from (None: no loan in this plan)
    same_as_cash: bool = False                # the safe lump covers the whole price, so mix is just cash


class BuyPlans(Base):
    profile_id: str
    vehicle_id: str
    vehicle_name: str
    price: float
    price_includes: str = "starting MSRP: before tax, destination and fees"
    credit_tier: str
    loan_apr: float                           # fraction
    savings_rate: float
    safe_lump_sum: float
    gross_monthly: float
    apr_source: SourceRef                     # the credit-tier row the rate was read from
    plans: dict[str, BuyPlan]
    recommended: PlanCode
    recommended_reason: Literal["loan_cheaper_than_savings", "cash_keeps_cushion", "cash_would_break_cushion", "no_safe_cash"]
    verdict: Verdict
    payment_ok: bool
    cushion_holds: bool
    verdict_cap: Literal["competes_with_high_apr_debt"] | None = None   # why the verdict was held at stretch
    high_apr_debt_open: bool = False          # waterfall step 3 (high-interest debt) is not done
    high_apr_debt_apr: float | None = None    # the dearest such debt's APR (fraction)
    caption: str = ""                         # the chart caption, on the same daily-low basis as months_p10_below_cushion
    baseline_month_low_p10: list[float] = Field(default_factory=list)   # each month's lowest daily p10, without the purchase
    scenario_month_low_p10: list[float] = Field(default_factory=list)   # ... and with the recommended plan
    baseline_monthly: list[MonthEndPoint] | None = None   # month-end p10 / p50 / p90 cash without the purchase (the recommended plan's run)
    scenario_monthly: list[MonthEndPoint] | None = None   # ... and with the recommended plan


class BuyPlansArgs(Base):
    vehicle_id: str | None = Field(None, description="A vehicle id from the research pack, e.g. honda_crv; omit for the cheapest car with a cited price")


def _plan(profile: Profile, base: sm.Baseline, code: PlanCode, price: float, down: float, apr: float, term: int, floor: float, slack: float,
          gross: float, liquid: float, max_share: float, src: SourceRef) -> tuple[BuyPlan, sm.SimulationResult | None]:
    loan = round(price - down, 2)
    pay = amortized_payment(loan, apr, term) if loan > 0 else 0.0
    interest = pay * term - loan if loan > 0 else 0.0
    if down > liquid + 0.005:                                      # more cash than there is: no forecast, no metrics
        miss = round(down - liquid, 2)
        return BuyPlan(code=code, name=NAMES[code], status="infeasible", down_payment=round(down, 2), loan_amount=loan, monthly_payment=0.0, term_months=0,
                       total_interest=0.0, total_cost=round(price, 2), cash_after=None, payment_share_of_gross=0.0, payment_ok=True,
                       cushion_floor=round(floor, 2), cushion=None, cushion_shortfall=None, cushion_holds=None,
                       warnings=[BuyWarning(code="not_enough_cash", amount=miss, detail=f"{miss:,.0f} more cash than the account holds")]), None
    scen = sm.Scenario(purchase_now=down or None, add_loan=sm.AddLoan(principal=loan, apr=apr, term_months=term, disbursed_to_cash=False) if loan > 0 else None)
    res = sm.simulate(profile, scen, baseline=base)
    short = max(0.0, floor - res.scenario_summary.min_p10)
    return BuyPlan(code=code, name=NAMES[code], down_payment=round(down, 2), loan_amount=loan, monthly_payment=round(pay, 2), term_months=term if loan > 0 else 0,
                   total_interest=round(interest, 2), total_cost=round(price + interest, 2), cash_after=round(liquid - down, 2),
                   payment_share_of_gross=round(pay / gross, 4), payment_ok=pay <= max_share * gross + 1e-9, cushion_floor=round(floor, 2),
                   cushion=res.scenario_summary, cushion_shortfall=round(short, 2), cushion_holds=short <= slack,
                   apr_source=src if loan > 0 else None,
                   warnings=[BuyWarning(code="overdraft_risk", amount=round(res.scenario_summary.min_p10, 2), detail="bad-luck cash goes below $0")]
                   if res.scenario_summary.min_p10 < 0 else []), res


def _money(x: float) -> str:
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def _caption(res: sm.SimulationResult, floor: float) -> str:
    """The chart caption, on the daily-low basis of min_p10 and months_p10_below_cushion: each month's lowest daily p10, not the month-end."""
    low_s, low_b = min(res.scenario_month_low_p10), min(res.baseline_month_low_p10)
    names = [m.day.strftime("%b") for m, low in zip(res.scenario_monthly or [], res.scenario_month_low_p10) if low < floor]
    tail = f"That is below the {_money(floor)} cushion target in {', '.join(names)}." if names else f"That stays above the {_money(floor)} cushion target."
    return f"In a bad stretch (10th percentile), the lowest daily cash is {_money(low_s)} with this plan and {_money(low_b)} without it. {tail}"


def _high_apr_debt(profile: Profile) -> tuple[bool, float | None]:
    """Waterfall step 3 open (high-interest debt not done), and the dearest such debt's APR."""
    step = next(x for x in rules.priority_waterfall(profile).steps if x.step == 3)
    if step.status in ("done", "not_applicable"):
        return False, None
    ids = set(step.detail.get("accounts", []))
    return True, max((a.rate for a in profile.accounts if a.id in ids), default=None)


def verdict_for(payment_ok: bool, cushion_holds: bool) -> Verdict:
    """affordable: both hold; not_now: neither; stretch: exactly one."""
    return "affordable" if payment_ok and cushion_holds else "not_now" if not payment_ok and not cushion_holds else "stretch"


def buy_plans_for(profile: Profile, vehicle_id: str | None, base: sm.Baseline | None = None) -> BuyPlans:
    v = pack.vehicle(vehicle_id) if vehicle_id else min((x for x in pack.vehicles() if pack.price(x) is not None), key=lambda x: pack.price(x))
    if v is None or pack.price(v) is None:
        raise ValueError(f"vehicle {vehicle_id!r} has no cited price in the research pack")
    T = {k: x["value"] for k, x in load_assumptions().items()}
    price = float(pack.price(v))
    apr, tier = pack.apr_for_score(profile.credit_score, "new")
    term, max_share = int(T["car_loan_term_months"]), float(T["car_payment_max_share_of_gross"])
    base = base or sm.make_baseline(profile)
    floor = float(base.cushion)
    slack = max(float(T["cushion_warn_min_shortfall"]), float(T["cushion_warn_min_shortfall_pct"]) * floor)
    liquid, gross = _liquid(profile), profile.gross_monthly
    safe = max(0.0, liquid - floor)
    down_mix = min(safe, price)
    rec_ = tier["apr_new"]
    src = SourceRef(source_title=rec_["source_title"], url=rec_["url"], fetched_at=rec_["fetched_at"])
    made = {code: _plan(profile, base, code, price, down, apr, term, floor, slack, gross, liquid, max_share, src)
            for code, down in (("cash", price), ("finance", 0.0), ("mix", down_mix))}
    plans = {k: m[0] for k, m in made.items()}
    plans["mix"].same_as_cash = down_mix >= price
    high_open, high_apr = _high_apr_debt(profile)
    if high_open:                                                   # cash spent here is cash not paying down the dearest debt
        for pl in plans.values():
            if pl.down_payment > 0 and pl.status == "ok":
                pl.warnings.append(BuyWarning(code="competes_with_high_apr_debt", amount=high_apr, detail="the cash could pay down debt at this rate"))
    savings = float(T["safe_savings_rate"])
    if apr > savings and plans["cash"].status == "ok" and plans["cash"].cushion_holds:
        rec, why = "cash", "cash_keeps_cushion"
    elif apr > savings and safe > 0:
        rec, why = "mix", "cash_would_break_cushion"
    elif apr > savings:
        rec, why = "finance", "no_safe_cash"
    else:
        rec, why = "finance", "loan_cheaper_than_savings"
    chosen = plans[rec]
    verdict = verdict_for(chosen.payment_ok, bool(chosen.cushion_holds))
    cap = None
    if high_open and verdict == "affordable":                       # step 3 open: not a clean yes while expensive debt is waiting
        verdict, cap = "stretch", "competes_with_high_apr_debt"
    res = made[rec][1]
    lows_b, lows_s = (res.baseline_month_low_p10, res.scenario_month_low_p10) if res else ([], [])
    return BuyPlans(profile_id=profile.id, vehicle_id=v["id"], vehicle_name=v["name"], price=price, credit_tier=tier["tier"], loan_apr=round(apr, 4),
                    savings_rate=savings, safe_lump_sum=round(safe, 2), gross_monthly=round(gross, 2), apr_source=src, plans=plans, recommended=rec,  # type: ignore[arg-type]
                    recommended_reason=why, verdict=verdict, payment_ok=chosen.payment_ok, cushion_holds=bool(chosen.cushion_holds),
                    verdict_cap=cap, high_apr_debt_open=high_open, high_apr_debt_apr=round(high_apr, 4) if high_apr is not None else None,
                    caption=_caption(res, floor) if res else "", baseline_month_low_p10=lows_b, scenario_month_low_p10=lows_s,
                    baseline_monthly=res.baseline_monthly if res else None, scenario_monthly=res.scenario_monthly if res else None)  # type: ignore[arg-type]


@register("buy_plans", args=BuyPlansArgs, result=BuyPlans, category="buy")
def buy_plans(profile: Profile, *, vehicle_id: str | None = None) -> BuyPlans:
    """Cash, finance and mix for one car from the research pack: payments, interest, cushion effect, verdict and the recommended plan."""
    return buy_plans_for(profile, vehicle_id)

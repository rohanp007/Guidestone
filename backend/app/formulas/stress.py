"""Emergency fund and job-loss stress test.

Reuses the liquid-cash forecast: the same paths, the same random draws, with each path's paychecks scaled by an income state from a
two-state Markov chain (employed / unemployed). Comparing the stressed run with the plain run on the same seed isolates the effect
of losing income. Assumptions (rates, benefits) live in assumptions.json with sources.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
from pydantic import Field

from ..config import assumptions as load_assumptions
from ..data.models import Base, Profile
from . import forecast as fc
from .registry import register


class EmergencyFund(Base):
    liquid_cash: float
    essential_monthly: float                 # housing, utilities, insurance, loan payments, groceries, healthcare, transport
    months_covered: float                    # liquid cash / essential_monthly
    target_months: float
    target_amount: float
    gap: float                               # how far below the target (0 if met)
    months_to_target: float | None           # at the current monthly surplus; None if there is no surplus or no gap


class StressScenario(Base):
    name: str
    description: str
    overdraft_probability: float             # share of paths whose liquid cash goes below $0 within the horizon
    cushion_probability: float               # ...below one month of essentials
    first_overdraft_p50: date | None         # first day the median path is below $0
    first_overdraft_p10: date | None
    months_until_overdraft_p50: float | None
    month_end_p50: list[float]               # first 12 month-ends
    month_end_p10: list[float]


class MarkovInfo(Base):
    monthly_job_loss: float
    monthly_reemployment: float
    expected_spell_months: float
    ui_income_share: float                   # unemployment benefit as a share of take-home pay
    ui_months: int
    probability_any_job_loss: float          # over the horizon
    average_months_unemployed: float


class JobLossStress(Base):
    profile_id: str
    applicable: bool                         # False for retirees (no paycheck to lose)
    note: str | None
    horizon_months: int
    paths: int
    emergency_fund: EmergencyFund
    markov: MarkovInfo | None
    scenarios: list[StressScenario]          # baseline, markov, lost_job_now
    assumptions: dict[str, dict]


class JobLossArgs(Base):
    horizon_months: int = Field(24, ge=3, le=60, description="Months to simulate")
    paths: int | None = Field(None, ge=100, le=10000, description="Monte Carlo paths; default from assumptions.json")
    seed: int = Field(0, description="Changes the random draws; the same arguments always give identical output")
    target_months: float | None = Field(None, ge=0.5, le=24, description="Emergency fund target in months of essentials; default from assumptions.json")


def _scenario(name: str, description: str, sim: fc.Sim, as_of: date) -> StressScenario:
    p10, p50 = sim.pct[0], sim.pct[1]
    first = lambda curve: fc._first_below(curve, 0.0, as_of)
    me = [(m - as_of).days for m in sim.schedule.month_ends][:12]
    f50 = first(p50)
    return StressScenario(
        name=name, description=description, overdraft_probability=sim.risks["overdraft"][1], cushion_probability=sim.risks["cushion"][1],
        first_overdraft_p50=f50, first_overdraft_p10=first(p10),
        months_until_overdraft_p50=(f50 - as_of).days / 30.4 if f50 else None,
        month_end_p50=[float(p50[i]) for i in me], month_end_p10=[float(p10[i]) for i in me])


def _markov_scale(rng: np.random.Generator, paths: int, months: int, p_loss: float, p_back: float, ui_share: float, ui_months: int):
    """(paths, months) income multipliers: 1 employed; ui_share while unemployed for fewer than ui_months months; 0 after benefits end."""
    employed, age = np.ones(paths, bool), np.zeros(paths, int)
    scale, ever, spent = np.ones((paths, months)), np.zeros(paths, bool), np.zeros(paths)
    for k in range(months):
        u = rng.random(paths)
        lose, back = employed & (u < p_loss), ~employed & (u < p_back)
        employed = (employed & ~lose) | back
        age = np.where(employed, 0, np.where(lose, 1, age + 1))
        scale[:, k] = np.where(employed, 1.0, np.where(age <= ui_months, ui_share, 0.0))
        ever |= ~employed
        spent += ~employed
    return scale, float(ever.mean()), float(spent.mean())


@register("job_loss_stress", args=JobLossArgs, result=JobLossStress, category="stress")
def job_loss_stress(profile: Profile, *, horizon_months: int = 24, paths: int | None = None, seed: int = 0,
                    target_months: float | None = None) -> JobLossStress:
    """Emergency fund coverage, and how long the customer's cash lasts if the paycheck stops.

    Runs the liquid-cash forecast three ways on the same random draws: unchanged, with income following a monthly employed /
    unemployed Markov chain (BLS flow rates, Pennsylvania unemployment benefits), and with the job lost now and no re-employment."""
    A_all = load_assumptions()
    A = {k: v["value"] for k, v in A_all.items()}
    used = {k: A_all[k] for k in ("job_loss_monthly", "reemployment_monthly", "ui_replacement", "ui_weekly_cap", "ui_max_months", "emergency_fund_months")}
    paths = int(paths or A["forecast_paths"])
    target = float(target_months or A["emergency_fund_months"])
    essential = fc.essential_monthly(profile)
    liquid = float(sum(a.balance for a in profile.accounts if a.is_liquid))
    surplus = profile.summary.monthly_surplus if profile.summary else 0.0
    gap = max(0.0, target * essential - liquid)
    fund = EmergencyFund(liquid_cash=liquid, essential_monthly=essential, months_covered=liquid / essential if essential > 0 else 0.0,
                         target_months=target, target_amount=target * essential, gap=gap,
                         months_to_target=gap / surplus if gap > 0 and surplus > 0 else None)
    base = dict(profile_id=profile.id, horizon_months=horizon_months, paths=paths, emergency_fund=fund, assumptions=used)
    if profile.employment == "retired":
        return JobLossStress(**base, applicable=False, note="Retired: income is a pension / Social Security, not a paycheck that can be lost.",
                             markov=None, scenarios=[])

    cal = fc._calibration()
    factors = cal["month_end_factors"] if cal else None
    key = fc._seed(profile.id, "stress", horizon_months, paths, seed)
    run = lambda scale: fc._simulate(profile, horizon_months, paths, essential, key, A, None, factors, scale)

    ui_share = min(1.0, min(A["ui_replacement"] * profile.gross_monthly, A["ui_weekly_cap"] * 52 / 12) / profile.net_monthly)
    ui_months = int(A["ui_max_months"])
    scale, p_any, avg_months = _markov_scale(np.random.default_rng(fc._seed(profile.id, "markov", seed)), paths, horizon_months,
                                             A["job_loss_monthly"], A["reemployment_monthly"], ui_share, ui_months)
    lost = np.tile(np.where(np.arange(1, horizon_months + 1) <= ui_months, ui_share, 0.0), (paths, 1))

    as_of = profile.as_of_date
    scenarios = [
        _scenario("baseline", "Income continues as forecast.", run(None), as_of),
        _scenario("markov", "Income follows the employed / unemployed chain: random job loss, benefits for up to "
                            f"{ui_months} months, then re-employment.", run(scale), as_of),
        _scenario("lost_job_now", f"Paycheck stops next month; unemployment benefits ({ui_share:.0%} of take-home) for {ui_months} months, "
                                  "then nothing, and no new job within the horizon.", run(lost), as_of),
    ]
    markov = MarkovInfo(monthly_job_loss=A["job_loss_monthly"], monthly_reemployment=A["reemployment_monthly"],
                        expected_spell_months=1 / A["reemployment_monthly"], ui_income_share=ui_share, ui_months=ui_months,
                        probability_any_job_loss=p_any, average_months_unemployed=avg_months)
    return JobLossStress(**base, applicable=True, note=None, markov=markov, scenarios=scenarios)

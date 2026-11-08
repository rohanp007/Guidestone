"""What-if simulation: the liquid-cash forecast with and without a scenario, on the SAME random draws.

A scenario (more spending, a new loan, a lump sum, extra money toward debt, a payoff strategy) becomes a list of dated deterministic
flows (forecast.Flow) that are added to the forecast's schedule before the Monte Carlo runs. Baseline and scenario use one seed, so every
path sees the same variable spend, and the difference between them is the scenario and nothing else (a no-op scenario differs by
exactly 0).

Debt payments: a debt that the payoff plan targets has its existing recurring payment REMOVED from the schedule (Flow.replaces) and
replaced by the plan's payments, so a payment is never counted twice. After the debt is paid off its payment stops (or has rolled over
to the next debt, which the plan's payments already contain).

Registered: simulate_scenario, simulate_payoff (full, with month-end bands for charts) and their *_summary variants (numbers only).
"""
from __future__ import annotations

import math
from datetime import timedelta
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..config import assumptions as load_assumptions
from ..data.models import Base, LiquidForecast, MonthEndPoint, Profile
from ..formulas import debt as debt_mod
from ..formulas import forecast as fc
from ..formulas import payoff
from ..formulas.debt import Amortization
from ..formulas.payoff import CushionImpact, PayoffWarning, SimDiff, SimSummary, StrategyResult
from ..formulas.registry import register

DAYS_PER_MONTH = 30.4375
MAX_HORIZON_MONTHS = 60                                     # the longest forecast horizon


# ---- the scenario ----------------------------------------------------------------------------------------------------------------

class ExpenseChange(Base):
    monthly_delta: float = Field(description="Change in monthly spending: positive spends more, negative spends less")
    start_month: int = Field(1, ge=1, description="First month it applies (1 = the month after today)")
    end_month: int | None = Field(None, ge=1, description="Last month it applies; omit for the whole horizon")

    @model_validator(mode="after")
    def _ordered(self):
        if self.end_month is not None and self.end_month < self.start_month:
            raise ValueError("end_month is before start_month")
        return self


class AddLoan(Base):
    principal: float = Field(gt=0)
    apr: float = Field(ge=0, le=1, description="Decimal: 0.07 = 7%")
    term_months: int = Field(ge=1, le=600)
    start_month: int = Field(1, ge=0, description="Month the loan starts (0 = tomorrow); the first payment is a month later")
    disbursed_to_cash: bool = Field(True, description="True: the principal lands in checking. False: it is spent elsewhere (a car, a house)")


class Scenario(Base):
    extra_monthly: float | None = Field(None, ge=0, description="Extra per month toward debt (with a payoff_strategy: None = the surplus)")
    expense_change: ExpenseChange | None = None
    add_loan: AddLoan | None = None
    lump_sum: float | None = Field(None, ge=0, description="One-time payment toward debt from cash at month 0 (with a payoff_strategy: None = safe lump sum)")
    payoff_strategy: str | None = Field(None, description="A avalanche, B snowball, C lump sum, D lump then avalanche, E minimums only, F custom")

    @field_validator("payoff_strategy")
    @classmethod
    def _code(cls, v):
        if v is not None and v not in payoff.CODES:
            raise ValueError(f"payoff_strategy must be one of {list(payoff.CODES)}")
        return v


# ---- scenario -> flows -----------------------------------------------------------------------------------------------------------

def payment_dates(profile: Profile, debt_id: str, months: int) -> list:
    """The dates the debt is paid on: its own recurring payment dates when the ledger shows them, else the next business day after
    each month's anniversary of as_of. One per month, starting next month."""
    as_of = profile.as_of_date
    end = fc.month_end(fc.add_months(as_of, months + 1))
    for r in profile.recurring:
        if r.kind in ("loan_payment", "card_payment") and (a := fc.debt_account(profile, r)) is not None and a.id == debt_id:
            dates = fc._occurrences(r, as_of, end, False)
            if r.cadence == "monthly" and len(dates) >= months:
                return dates[:months]
    return [fc.next_bd(fc.add_months(as_of, k)) for k in range(1, months + 1)]


def payoff_flows(profile: Profile, schedule: Amortization, horizon_months: int = MAX_HORIZON_MONTHS) -> list[fc.Flow]:
    """The payoff plan as cash flows. Every debt in the plan gets a zero-amount marker flow that replaces its recurring payment, so a
    debt the lump sum clears on day 1 (no payments at all) still loses its old payment."""
    as_of, flows = profile.as_of_date, []
    months = min(max((r.month for r in schedule.schedule), default=0), horizon_months)
    dates = {i: payment_dates(profile, i, max(months, 1)) for i in schedule.per_debt}
    for i in schedule.per_debt:
        flows.append(fc.Flow(as_of + timedelta(days=1), 0.0, f"{i} payoff plan replaces its regular payment", "payoff", replaces=i))
    for row in schedule.schedule:
        if row.month > horizon_months:
            break
        if row.month == 0:
            if row.total_payment > 0:
                flows.append(fc.Flow(as_of + timedelta(days=1), -row.total_payment, "Payoff lump sum", "payoff_lump"))
            continue
        for i, amount in row.payments.items():
            if amount > 0:
                flows.append(fc.Flow(dates[i][row.month - 1], -amount, f"Payoff plan: {i}", "payoff", replaces=i))
    return flows


def to_changes(profile: Profile, scenario: Scenario, schedule: Amortization | None = None,
               horizon_months: int = MAX_HORIZON_MONTHS) -> list[fc.Flow]:
    """The scenario as dated flows, for the next `horizon_months`. `schedule` is the payoff plan's amortization, if there is one (its
    debts' regular payments are replaced by the plan's payments)."""
    as_of, flows = profile.as_of_date, []
    if schedule is not None:
        flows += payoff_flows(profile, schedule, horizon_months)
    ec = scenario.expense_change
    if ec is not None and ec.monthly_delta:
        for k in range(ec.start_month, min(ec.end_month or horizon_months, horizon_months) + 1):
            flows.append(fc.Flow(fc.add_months(as_of, k), -ec.monthly_delta, "Spending change", "expense_change"))
    loan = scenario.add_loan
    if loan is not None:
        first = fc.add_months(as_of, loan.start_month) if loan.start_month else as_of + timedelta(days=1)
        if loan.disbursed_to_cash:
            flows.append(fc.Flow(first, loan.principal, "New loan proceeds", "new_loan"))
        pay = debt_mod.amortized_payment(loan.principal, loan.apr, loan.term_months)
        for j in range(1, min(loan.term_months, horizon_months - loan.start_month) + 1):
            flows.append(fc.Flow(fc.next_bd(fc.add_months(as_of, loan.start_month + j)), -pay, "New loan payment", "new_loan"))
    return flows


# ---- paired runs -----------------------------------------------------------------------------------------------------------------

@dataclass
class Baseline:
    """The forecast as it stands, run once and reused. `seed` is shared with every scenario run against it."""
    profile_id: str
    days: int
    horizon_months: int
    paths: int
    seed: int
    cushion: float
    A_all: dict
    A: dict
    risk_factors: list[float] | None
    forecast: LiquidForecast


def _run(profile: Profile, base: "Baseline", changes: list[fc.Flow]) -> LiquidForecast:
    sim = fc._simulate(profile, base.horizon_months, base.paths, base.cushion, base.seed, base.A, changes or None, base.risk_factors)
    return fc._summarize(profile, sim, base.horizon_months, base.A_all, daily_days=base.horizon_months * 31)       # daily bands for every day


def make_baseline(profile: Profile, days: int = 180, paths: int | None = None, seed: int = 0) -> Baseline:
    A_all = load_assumptions()
    A = {k: v["value"] for k, v in A_all.items()}
    paths = int(paths or A["forecast_paths"])
    cal = fc._calibration() if fc.OPTIONS["calibrate"] else None
    base = Baseline(profile.id, days, max(1, math.ceil(days / DAYS_PER_MONTH)), paths, fc._seed(profile.id, "simulate", seed),
                    float(A["cushion_months"] * fc.essential_monthly(profile)), A_all, A, cal["month_end_factors"] if cal else None,
                    None)  # type: ignore[arg-type]
    base.forecast = _run(profile, base, [])
    return base


def month_min_p10(lf: LiquidForecast) -> list[float]:
    """For each forecast month, the lowest DAILY p10 balance in it (the pessimistic band on its worst day), not the balance on the last day.
    Paydays often fall on the last business day, so month-end points sit just after income arrives and hide the dip before it. The month
    runs from the day after the previous month-end (or today) to its own month-end. A month the daily band does not reach falls back to
    its month-end p10."""
    daily, out, prev = (lf.daily.p10 if lf.daily else []), [], lf.as_of_date
    for m in lf.monthly or []:
        lo, hi = (prev - lf.as_of_date).days + 1, min((m.day - lf.as_of_date).days, len(daily) - 1)
        out.append(min(daily[lo:hi + 1]) if daily and lo <= hi else m.p10)
        prev = m.day
    return out


def summarize(lf: LiquidForecast, cushion: float) -> SimSummary:
    months, lows = lf.monthly or [], month_min_p10(lf)
    return SimSummary(end_p50=months[-1].p50, min_p10=min(lows), overdraft_prob=lf.overdraft_risk.probability,
                      cushion_prob=lf.cushion_risk.probability, months_p10_below_cushion=sum(low < cushion for low in lows))


def diff(a: SimSummary, b: SimSummary) -> SimDiff:
    """b - a."""
    return SimDiff(**{k: getattr(b, k) - getattr(a, k) for k in SimSummary.model_fields})


class SimulationResult(Base):
    profile_id: str
    days: int
    horizon_months: int
    paths: int
    cushion: float
    baseline_summary: SimSummary
    scenario_summary: SimSummary
    diff: SimDiff                                            # scenario - baseline
    baseline_monthly: list[MonthEndPoint] | None             # month-end p10 / p50 / p90 (calibrated), for charts
    scenario_monthly: list[MonthEndPoint] | None
    flows: int                                               # dated flows the scenario added (0: a no-op)
    plan: StrategyResult | None = None                       # the payoff plan behind the flows, if any
    warnings: list[PayoffWarning] = Field(default_factory=list)


def simulate(profile: Profile, scenario: Scenario, days: int = 180, *, schedule: Amortization | None = None, baseline: Baseline | None = None,
             paths: int | None = None, seed: int = 0) -> SimulationResult:
    """Baseline and scenario forecasts on the same random draws. `schedule`: a payoff plan to use as-is (otherwise the scenario's own
    payoff fields build one). `baseline`: a run to reuse (otherwise made here)."""
    base = baseline or make_baseline(profile, days, paths, seed)
    plan, warnings = None, []
    if schedule is None:
        plan = payoff.plan_for_scenario(profile, strategy=scenario.payoff_strategy, extra_monthly=scenario.extra_monthly, lump_sum=scenario.lump_sum)
        if plan is None and (scenario.payoff_strategy or scenario.extra_monthly or scenario.lump_sum):
            warnings.append(PayoffWarning(code="no_debt", detail="no debt in scope: the payoff fields changed nothing"))
        if plan is not None:
            schedule, warnings = plan.amortization, list(plan.warnings)
    flows = to_changes(profile, scenario, schedule, base.horizon_months)
    scen = _run(profile, base, flows) if flows else base.forecast          # no flows: nothing differs, so the runs are the same run
    b, s = summarize(base.forecast, base.cushion), summarize(scen, base.cushion)
    return SimulationResult(profile_id=profile.id, days=base.days, horizon_months=base.horizon_months, paths=base.paths, cushion=round(base.cushion, 2),
                            baseline_summary=b, scenario_summary=s, diff=diff(b, s), baseline_monthly=base.forecast.monthly,
                            scenario_monthly=scen.monthly, flows=len(flows), plan=plan, warnings=warnings)


def cushion_impact(profile: Profile, schedule: Amortization, baseline: Baseline) -> CushionImpact:
    """What a payoff plan's payments do to the cash cushion, against the baseline forecast."""
    r = simulate(profile, Scenario(), schedule=schedule, baseline=baseline)
    return CushionImpact(cushion=r.cushion, horizon_months=r.horizon_months, paths=r.paths, baseline=r.baseline_summary,
                         scenario=r.scenario_summary, diff=r.diff)


# ---- registered formulas ---------------------------------------------------------------------------------------------------------

class SimulationSummary(Base):
    profile_id: str
    horizon_months: int
    cushion: float
    baseline: SimSummary
    scenario: SimSummary
    diff: SimDiff
    flows: int
    payoff: payoff.StrategySummary | None
    warnings: list[PayoffWarning]


def _summary(r: SimulationResult) -> SimulationSummary:
    return SimulationSummary(profile_id=r.profile_id, horizon_months=r.horizon_months, cushion=r.cushion, baseline=r.baseline_summary,
                             scenario=r.scenario_summary, diff=r.diff, flows=r.flows, warnings=r.warnings,
                             payoff=payoff.summarize_strategy(r.plan) if r.plan else None)


@register("simulate_scenario", args=Scenario, result=SimulationResult, category="simulate")
def simulate_scenario(profile: Profile, *, extra_monthly: float | None = None, expense_change: ExpenseChange | None = None,
                      add_loan: AddLoan | None = None, lump_sum: float | None = None, payoff_strategy: str | None = None) -> SimulationResult:
    """Runs the liquid-cash forecast with a what-if (spending change, new loan, lump sum or extra money toward debt, payoff strategy)
    and without it, on the same random draws, and returns both with their difference. Full result, with month-end bands."""
    return simulate(profile, Scenario(extra_monthly=extra_monthly, expense_change=expense_change, add_loan=add_loan, lump_sum=lump_sum,
                                      payoff_strategy=payoff_strategy))


@register("simulate_scenario_summary", args=Scenario, result=SimulationSummary, category="simulate")
def simulate_scenario_summary(profile: Profile, **kw) -> SimulationSummary:
    """Same what-if as simulate_scenario, as numbers only: no month-end bands, no schedules."""
    return _summary(simulate_scenario(profile, **kw))


class SimulatePayoffArgs(Base):
    strategy: payoff.Code | None = Field(None, description="Omit to use the recommended strategy (A if none). A avalanche, B snowball, C lump sum then minimums, D lump sum then avalanche, E minimums only, F custom")
    extra_monthly: float | None = Field(None, ge=0, description="Extra per month on top of minimums; omit to use the monthly surplus, 0 means none")
    lump_sum: float | None = Field(None, ge=0, description="One-time payment from cash at month 0; omit to use the safe lump sum, 0 means none")
    order: list[str] | None = Field(None, description="Account ids in the order extra money should go (strategy F)")


class PayoffSimulation(Base):
    no_debt: bool = False                                    # nothing in scope to pay off: strategy and simulation are None
    strategy: StrategyResult | None = None
    simulation: SimulationResult | None = None


def simulate_payoff_run(profile: Profile, *, strategy: str | None = None, extra_monthly: float | None = None, lump_sum: float | None = None,
                        order: list[str] | None = None, include_mortgage: bool = False) -> PayoffSimulation:
    # a mortgage the caller names in `order` is an explicit request to pay it
    include_mortgage = include_mortgage or any(a.type == "mortgage" and a.id in (order or []) for a in profile.accounts)
    ctx = payoff.make_context(profile, extra_monthly=extra_monthly, lump_sum=lump_sum, include_mortgage=include_mortgage)
    if not ctx.debts:
        return PayoffSimulation(no_debt=True)
    strategy = strategy or payoff.recommend(ctx).code or "A"           # the recommended strategy, A if there is none
    base = make_baseline(profile)
    result = payoff._result(ctx, strategy, order, base)
    sim = simulate(profile, Scenario(), schedule=result.amortization, baseline=base)
    return PayoffSimulation(strategy=result, simulation=sim.model_copy(update={"plan": None}))


@register("simulate_payoff", args=SimulatePayoffArgs, result=PayoffSimulation, category="simulate")
def simulate_payoff(profile: Profile, *, strategy: str | None = None, extra_monthly: float | None = None, lump_sum: float | None = None,
                    order: list[str] | None = None) -> PayoffSimulation:
    """One payoff strategy in detail: its month-by-month schedule, and the liquid-cash forecast with its payments against the forecast
    as it stands (month-end bands for charts). Full result."""
    return simulate_payoff_run(profile, strategy=strategy, extra_monthly=extra_monthly, lump_sum=lump_sum, order=order)


class PayoffSimulationSummary(Base):
    no_debt: bool
    strategy: payoff.StrategySummary | None
    simulation: SimulationSummary | None


@register("simulate_payoff_summary", args=SimulatePayoffArgs, result=PayoffSimulationSummary, category="simulate")
def simulate_payoff_summary(profile: Profile, **kw) -> PayoffSimulationSummary:
    """Same as simulate_payoff, as numbers only: months, interest, saved, cushion difference, warnings. No schedules or bands."""
    r = simulate_payoff(profile, **kw)
    if r.strategy is None or r.simulation is None:
        return PayoffSimulationSummary(no_debt=True, strategy=None, simulation=None)
    return PayoffSimulationSummary(no_debt=False, strategy=payoff.summarize_strategy(r.strategy), simulation=_summary(r.simulation))

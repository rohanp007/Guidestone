"""Payoff strategies A-F: the same debts paid down in different ways, compared with each other and with paying only the minimums.

    A avalanche        extra money goes to the highest APR first
    B snowball         extra money goes to the smallest balance first (reports snowball_extra_cost = B interest - A interest)
    C lump_sum         a lump sum at month 0 (avalanche order), then minimums only
    D lump_avalanche   the lump sum at month 0, then A
    E minimum_only     minimums only: the baseline every saving is measured against
    F custom           the caller's order and / or lump sum and extra money

`extra_monthly`: None = the monthly surplus (the way payoff_priority resolves it), 0 = none. `lump_sum`: None = rules.py's safe lump
sum (liquid cash above the cushion floor), 0 = none; a larger one still runs, with an "exceeds_safe_lump" warning.

Which strategy is recommended is decided here from payoff_priority's output (code, not a model). Every strategy also reports what it
does to the cash cushion, by running the liquid-cash forecast with that strategy's payments (engine/simulate.py).

Registered: payoff_strategies and simulate_payoff (full, with schedules and chart bands) and their *_summary variants (numbers only,
for agents).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from ..config import assumptions as load_assumptions
from ..data.models import Base, Profile
from . import forecast as fc
from . import rules
from .debt import Amortization, Debt, amortize, debts_in_scope, excluded_mortgages
from .registry import register

CODES = {"A": "avalanche", "B": "snowball", "C": "lump_sum", "D": "lump_avalanche", "E": "minimum_only", "F": "custom"}
Code = Literal["A", "B", "C", "D", "E", "F"]


# ---- result models (the simulation summaries live here so engine/simulate.py can import them without a cycle) ------------------------

class PayoffWarning(Base):
    code: str                                 # exceeds_safe_lump, lump_exceeds_debt, tradeoff_low_cushion, never_pays_off, hit_max_months, no_surplus, ...
    amount: float | None = None
    limit: float | None = None
    unused_amount: float | None = None        # lump_exceeds_debt: the part of the lump sum the debts could not take
    accounts: list[str] = Field(default_factory=list)
    detail: str = ""


class SimSummary(Base):
    end_p50: float                            # median liquid cash at the last month end
    min_p10: float                            # lowest p10 (the pessimistic band) of any day in the window: each month's minimum DAILY balance
    overdraft_prob: float                     # probability of dropping below $0 at some point
    cushion_prob: float                       # ...below the cushion target
    months_p10_below_cushion: int                 # months whose lowest daily p10 balance (the same one min_p10 is the lowest of) is below the cushion target


class SimDiff(Base):
    end_p50: float
    min_p10: float
    overdraft_prob: float
    cushion_prob: float
    months_p10_below_cushion: int


class CushionImpact(Base):
    cushion: float                            # the rules.py cushion target: cushion_months x essential monthly expenses
    horizon_months: int
    paths: int                                # Monte Carlo paths behind these numbers
    baseline: SimSummary                      # the forecast as it stands
    scenario: SimSummary                      # the forecast with this strategy's payments
    diff: SimDiff                             # scenario - baseline, same random draws in both


class StrategyResult(Base):
    code: str                                 # A-F
    name: str
    order: list[str]
    extra_monthly: float
    lump_sum: float                           # the lump sum asked for / resolved
    lump_applied: float                       # what the debts could take (never more than they owe)
    safe_lump_sum: float                      # rules.py's safe lump sum, for comparison
    amortization: Amortization
    interest_saved_vs_minimum: float          # vs E (E's interest is a lower bound when E is capped: see minimum_only_capped)
    months_saved_vs_minimum: int | None
    snowball_extra_cost: float | None = None  # B only
    cushion_impact: CushionImpact | None = None
    warnings: list[PayoffWarning] = Field(default_factory=list)


class ComparisonRow(Base):
    code: str
    name: str
    months: int | None
    total_interest: float
    interest_saved: float
    cushion_min_p10: float | None
    months_p10_below_cushion: int | None


class Recommendation(Base):
    code: str | None                          # None: nothing to recommend (no debt)
    reason_code: str                          # payoff_priority's reason (high_apr, optional, mortgage_extra) or no_debt / keep_cash_earns_more
    name: str | None = None


class PayoffStrategies(Base):
    profile_id: str
    no_debt: bool                             # nothing in scope to pay down
    no_surplus: bool                          # payoff_priority's flag: surplus <= 0
    flags: list[str]                          # payoff_priority's flags
    include_mortgage: bool
    excluded_mortgages: list[str]
    debts: list[Debt]
    extra_monthly: float                      # resolved
    safe_lump_sum: float
    lump_sum: float                           # resolved
    strategies: dict[str, StrategyResult]
    comparison: list[ComparisonRow]
    recommended: Recommendation | None
    snowball_extra_cost: float | None
    cushion: float | None
    paths: int | None                         # Monte Carlo paths used for the cushion numbers
    runtime_ms: float


class StrategySummary(Base):
    code: str
    name: str
    months: int | None
    payoff_date: str | None
    total_interest: float
    interest_saved: float
    months_saved: int | None
    cushion_min_p10: float | None
    cushion_min_p10_diff: float | None
    months_p10_below_cushion: int | None
    months_p10_below_cushion_diff: int | None
    warnings: list[PayoffWarning]


class PayoffStrategiesSummary(Base):
    profile_id: str
    no_debt: bool
    no_surplus: bool
    extra_monthly: float
    lump_sum: float
    safe_lump_sum: float
    recommended: Recommendation | None
    snowball_extra_cost: float | None
    strategies: list[StrategySummary]
    cushion: float | None
    excluded_mortgages: list[str] = []        # mortgages left out of scope (the plan can be rerun with include_mortgage=True)
    highest_debt_apr: float | None = None     # the dearest debt in scope
    highest_debt_type: str | None = None      # that debt's account type, e.g. "mortgage"
    savings_rate: float | None = None         # the customer's own high-yield rate when they hold one at or above the benchmark, else the benchmark
    holds_hys: bool = False                   # whether they already hold a high-yield savings account at or above the benchmark rate


# ---- orders ----------------------------------------------------------------------------------------------------------------------

def avalanche_order(debts: list[Debt]) -> list[str]:
    """APR high to low; equal APR: cards before loans, then the smaller balance."""
    return [d.id for d in sorted(debts, key=lambda d: (-round(d.apr, 6), d.kind != "card", d.balance, d.id))]


def snowball_order(debts: list[Debt]) -> list[str]:
    return [d.id for d in sorted(debts, key=lambda d: (d.balance, d.id))]


# ---- running strategies ----------------------------------------------------------------------------------------------------------

@dataclass
class Context:
    profile: Profile
    debts: list[Debt]
    include_mortgage: bool
    rollover: bool
    extra: float                              # resolved extra monthly
    extra_given: bool
    lump: float                               # resolved lump sum
    safe_lump: float
    priority: rules.PayoffPriority
    minimum: Amortization                     # strategy E
    safe_savings_rate: float
    low_floor: float | None                   # cash floor for lump sums when the card exception applies (low_cushion_months of essentials)


def make_context(profile: Profile, *, extra_monthly: float | None = None, lump_sum: float | None = None, include_mortgage: bool = False,
                 rollover: bool = True) -> Context:
    """Resolve the arguments once: debts in scope, extra money (None -> surplus), lump sum (None -> safe lump sum), and strategy E."""
    debts = debts_in_scope(profile, include_mortgage)
    pp = rules.payoff_priority(profile, lump_sum=0.0, extra_monthly=extra_monthly)      # as-is: resolves the surplus, finds the safe lump sum
    lump = pp.safe_lump_sum if lump_sum is None else lump_sum
    minimum = amortize(debts, 0.0, avalanche_order(debts), rollover=rollover)
    return Context(profile, debts, include_mortgage, rollover, pp.extra_monthly_applied, extra_monthly is not None, lump, pp.safe_lump_sum,
                   pp, minimum, load_assumptions()["safe_savings_rate"]["value"],
                   None if pp.safe_lump_sum_low_cushion is None else
                   load_assumptions()["low_cushion_months"]["value"] * fc.essential_monthly(profile))


def validate_order(debts: list[Debt], order: list[str]) -> list[str]:
    ids = {d.id for d in debts}
    bad = [i for i in order if i not in ids]
    if bad:
        raise ValueError(f"order has ids that are not in-scope debts: {bad}; in scope: {sorted(ids)}")
    if len(set(order)) != len(order):
        raise ValueError("order lists a debt more than once")
    return order


def _plan(ctx: Context, code: str, order: list[str] | None) -> tuple[Amortization, float, float]:
    """(amortization, extra, lump) for one strategy."""
    base = avalanche_order(ctx.debts)
    if code == "A":
        extra, lump, ordr = ctx.extra, 0.0, base
    elif code == "B":
        extra, lump, ordr = ctx.extra, 0.0, snowball_order(ctx.debts)
    elif code == "C":
        extra, lump, ordr = 0.0, ctx.lump, base
    elif code == "D":
        extra, lump, ordr = ctx.extra, ctx.lump, base
    elif code == "E":
        extra, lump, ordr = 0.0, 0.0, base
    elif code == "F":
        extra, lump, ordr = ctx.extra, ctx.lump, validate_order(ctx.debts, order or []) or base
    else:
        raise ValueError(f"unknown strategy {code!r}; use one of {list(CODES)}")
    return amortize(ctx.debts, extra, ordr, lump_sum=lump, rollover=ctx.rollover), extra, lump


def _warnings(ctx: Context, am: Amortization, extra: float, lump: float, uses_extra: bool) -> list[PayoffWarning]:
    out: list[PayoffWarning] = []
    if am.lump_applied > ctx.safe_lump + 0.005:
        out.append(PayoffWarning(code="exceeds_safe_lump", amount=round(am.lump_applied - ctx.safe_lump, 2), limit=round(ctx.safe_lump, 2),
                                 detail="the lump sum applied is over the amount that keeps the cushion floor"))
    if lump - am.lump_applied > 0.005:
        out.append(PayoffWarning(code="lump_exceeds_debt", unused_amount=round(lump - am.lump_applied, 2), limit=round(ctx.safe_lump, 2),
                                 detail=f"lump sum {lump:,.2f} (safe lump sum {ctx.safe_lump:,.2f}) but the debts take only {am.lump_applied:,.2f}; the rest stays in cash"))
    if uses_extra and not ctx.extra_given and ctx.extra <= 0:                    # nothing to put toward the debt each month
        out.append(PayoffWarning(code="no_surplus"))
    if am.never_pays_off:
        out.append(PayoffWarning(code="never_pays_off", accounts=[i for i, p in am.per_debt.items() if p.never_pays_off],
                                 detail="a payment does not cover its interest"))
    if am.hit_max_months:
        out.append(PayoffWarning(code="hit_max_months", amount=float(am.max_months), detail="not cleared within the simulation limit"))
    cheap = [d.id for d in ctx.debts if d.apr < ctx.safe_savings_rate]
    if cheap and (extra > 0 or lump > 0):
        out.append(PayoffWarning(code="keep_cash_earns_more", accounts=cheap, detail="these debts cost less than savings earn"))
    if ctx.minimum.months_to_payoff is None:
        out.append(PayoffWarning(code="minimum_only_capped", detail="minimum-only does not clear within the limit, so savings are a lower bound"))
    return out


def _result(ctx: Context, code: str, order: list[str] | None, baseline=None, *, cushion: bool = True, paths: int | None = None,
            days: int = 180) -> StrategyResult:
    am, extra, lump = _plan(ctx, code, order)
    E = ctx.minimum
    e_months = E.months_to_payoff if E.months_to_payoff is not None else E.max_months       # a capped E counts as max_months
    result = StrategyResult(
        code=code, name=CODES[code], order=am.order, extra_monthly=round(extra, 2), lump_sum=round(lump, 2), lump_applied=am.lump_applied,
        safe_lump_sum=round(ctx.safe_lump, 2), amortization=am,
        interest_saved_vs_minimum=round(E.total_interest - am.total_interest, 2),
        months_saved_vs_minimum=None if am.months_to_payoff is None else e_months - am.months_to_payoff,
        warnings=_warnings(ctx, am, extra, lump, code in "ABDF"))
    if cushion and ctx.debts:
        from ..engine import simulate as sim                  # imported here: simulate imports this module for the models above
        base = baseline or sim.make_baseline(ctx.profile, days=days, paths=paths)
        ci = sim.cushion_impact(ctx.profile, am, base)
        floor = ctx.low_floor if ctx.low_floor is not None else ci.cushion      # the lowest cash the rules allow
        A = load_assumptions()
        material = max(A["cushion_warn_min_shortfall"]["value"], A["cushion_warn_min_shortfall_pct"]["value"] * ci.cushion)
        shortfall = ci.cushion - ci.scenario.min_p10                    # months_p10_below_cushion stays the raw count; the warning needs a real dip
        if shortfall > material and ci.scenario.min_p10 >= floor:
            result.warnings.append(PayoffWarning(
                code="tradeoff_low_cushion", amount=round(ci.scenario.min_p10, 2), limit=round(floor, 2),
                detail=f"months_p10_below_cushion={ci.scenario.months_p10_below_cushion}, cushion_min_p10={ci.scenario.min_p10:,.2f}; "
                       f"below the {ci.cushion:,.2f} cushion target but not below the {floor:,.2f} floor"))
        result = result.model_copy(update={"cushion_impact": ci})
    return result


def recommend(ctx: Context) -> Recommendation:
    """payoff_priority decides: its ordered debts and flags pick the strategy, its reason code is passed through."""
    pp = ctx.priority
    if not ctx.debts:
        return Recommendation(code=None, reason_code="no_debt")
    if not pp.order:
        return Recommendation(code="E", reason_code="keep_cash_earns_more", name=CODES["E"])      # every debt is cheaper than savings earn
    use_lump, use_extra = ctx.lump > 0, ctx.extra > 0
    code = "D" if use_lump and use_extra else "C" if use_lump else "A" if use_extra else "E"
    return Recommendation(code=code, reason_code=pp.order[0].reason_code if code != "E" else "no_surplus", name=CODES[code])


# ---- public functions ------------------------------------------------------------------------------------------------------------

def payoff_strategies(profile: Profile, *, extra_monthly: float | None = None, lump_sum: float | None = None, include_mortgage: bool = False,
                      rollover: bool = True, order: list[str] | None = None, paths: int | None = None, days: int = 180) -> PayoffStrategies:
    """Runs strategies A-E (and F when `order` is given) over the debts in scope. The liquid-cash baseline forecast runs once and
    every strategy is compared with it on the same random draws."""
    started = time.perf_counter()
    ctx = make_context(profile, extra_monthly=extra_monthly, lump_sum=lump_sum, include_mortgage=include_mortgage, rollover=rollover)
    flags = list(ctx.priority.flags)
    head: dict[str, Any] = dict(profile_id=profile.id, no_debt=not ctx.debts, no_surplus="no_surplus" in flags, flags=flags, include_mortgage=include_mortgage,
                excluded_mortgages=excluded_mortgages(profile, include_mortgage), debts=ctx.debts, extra_monthly=round(ctx.extra, 2),
                safe_lump_sum=round(ctx.safe_lump, 2), lump_sum=round(ctx.lump, 2))
    if not ctx.debts:
        return PayoffStrategies(**head, strategies={}, comparison=[], recommended=recommend(ctx), snowball_extra_cost=None, cushion=None,
                                paths=None, runtime_ms=round(1000 * (time.perf_counter() - started), 1))
    from ..engine import simulate as sim
    base = sim.make_baseline(profile, days=days, paths=paths)
    codes = ["A", "B", "C", "D", "E"] + (["F"] if order else [])
    results = {c: _result(ctx, c, order, base) for c in codes}
    cost = round(results["B"].amortization.total_interest - results["A"].amortization.total_interest, 2)
    results["B"] = results["B"].model_copy(update={"snowball_extra_cost": cost})
    rows = []
    for c, r in results.items():
        ci = r.cushion_impact
        rows.append(ComparisonRow(code=c, name=r.name, months=r.amortization.months_to_payoff, total_interest=r.amortization.total_interest,
                                  interest_saved=r.interest_saved_vs_minimum, cushion_min_p10=ci.scenario.min_p10 if ci else None,
                                  months_p10_below_cushion=ci.scenario.months_p10_below_cushion if ci else None))
    return PayoffStrategies(**head, strategies=results, comparison=rows, recommended=recommend(ctx), snowball_extra_cost=cost,
                            cushion=base.cushion, paths=base.paths, runtime_ms=round(1000 * (time.perf_counter() - started), 1))


def summarize_strategy(r: StrategyResult) -> StrategySummary:
    am, ci = r.amortization, r.cushion_impact
    return StrategySummary(code=r.code, name=r.name, months=am.months_to_payoff, payoff_date=am.payoff_date.isoformat() if am.payoff_date else None,
                           total_interest=am.total_interest, interest_saved=r.interest_saved_vs_minimum, months_saved=r.months_saved_vs_minimum,
                           cushion_min_p10=ci.scenario.min_p10 if ci else None, cushion_min_p10_diff=ci.diff.min_p10 if ci else None,
                           months_p10_below_cushion=ci.scenario.months_p10_below_cushion if ci else None,
                           months_p10_below_cushion_diff=ci.diff.months_p10_below_cushion if ci else None, warnings=r.warnings)


def plan_for_scenario(profile: Profile, *, strategy: str | None, extra_monthly: float | None, lump_sum: float | None,
                      include_mortgage: bool = False, order: list[str] | None = None) -> StrategyResult | None:
    """The payoff plan a what-if scenario asks for, without the cushion run (the scenario forecast does that itself). With a named
    strategy, None extra / lump mean that strategy's defaults (surplus / safe lump sum). Without one, None means none: extra money
    alone is avalanche (A), a lump sum alone is C, both is D. Nothing asked for -> None."""
    if strategy is None:
        if not (extra_monthly or lump_sum):
            return None
        code = "D" if extra_monthly and lump_sum else "C" if lump_sum else "A"
        extra_monthly, lump_sum = extra_monthly or 0.0, lump_sum or 0.0
    else:
        code = strategy
    ctx = make_context(profile, extra_monthly=extra_monthly, lump_sum=lump_sum, include_mortgage=include_mortgage)
    if not ctx.debts:
        return None
    return _result(ctx, code, order, cushion=False)


class PayoffStrategiesArgs(Base):
    extra_monthly: float | None = Field(None, ge=0, description="Extra per month on top of minimums; omit to use the monthly surplus, 0 means none")
    lump_sum: float | None = Field(None, ge=0, description="One-time payment from cash at month 0; omit to use the safe lump sum, 0 means none")
    include_mortgage: bool = Field(False, description="Count the mortgage as a debt to pay down")


@register("payoff_strategies", args=PayoffStrategiesArgs, result=PayoffStrategies, category="payoff")
def payoff_strategies_formula(profile: Profile, *, extra_monthly: float | None = None, lump_sum: float | None = None,
                              include_mortgage: bool = False) -> PayoffStrategies:
    """Compares paying off debt by avalanche, snowball, a lump sum, lump sum then avalanche, and minimums only: months to debt-free,
    interest, interest saved against minimums only, and what each does to the cash cushion. Full result, with schedules."""
    return payoff_strategies(profile, extra_monthly=extra_monthly, lump_sum=lump_sum, include_mortgage=include_mortgage)


@register("payoff_strategies_summary", args=PayoffStrategiesArgs, result=PayoffStrategiesSummary, category="payoff")
def payoff_strategies_summary(profile: Profile, *, extra_monthly: float | None = None, lump_sum: float | None = None,
                              include_mortgage: bool = False) -> PayoffStrategiesSummary:
    """Same comparison as payoff_strategies, as numbers only: no schedules, small enough for an agent's context."""
    full = payoff_strategies(profile, extra_monthly=extra_monthly, lump_sum=lump_sum, include_mortgage=include_mortgage)
    top = max(full.debts, key=lambda d: d.apr, default=None)
    bench = float(load_assumptions()["safe_savings_rate"]["value"])
    held = max((a.rate for a in profile.accounts if a.type == "savings" and a.subtype == "high_yield" and a.rate >= bench), default=None)
    rate = held if held is not None else bench
    return PayoffStrategiesSummary(profile_id=full.profile_id, no_debt=full.no_debt, no_surplus=full.no_surplus, extra_monthly=full.extra_monthly,
                                   lump_sum=full.lump_sum, safe_lump_sum=full.safe_lump_sum, recommended=full.recommended,
                                   snowball_extra_cost=full.snowball_extra_cost, cushion=full.cushion,
                                   excluded_mortgages=full.excluded_mortgages, highest_debt_apr=top.apr if top else None,
                                   highest_debt_type=top.type if top else None, savings_rate=rate, holds_hys=held is not None,
                                   strategies=[summarize_strategy(r) for r in full.strategies.values()])

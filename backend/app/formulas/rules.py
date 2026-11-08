"""Financial decision rules (docs/rules.md): priority_waterfall, payoff_priority, candidate_moves.

Deterministic. Every threshold is read from assumptions.json (docs/rules.md section 6); there are no magic numbers here. The advisor
explains and phrases these results; it never overrides them.

Interpretations the rules text leaves open (each is flagged where it is used):
- "Debt" means a liability with a balance, EXCEPT a pay-in-full card: its balance is the current statement, paid off in full every
  month, so it carries no interest and is not a debt to pay down. (41 of the dataset's 44 pay-in-full cards show such a balance.)
- Mortgages never count in waterfall step 3 (extra mortgage payments are step 7 / payoff rule 4: only above mortgage_extra_rate
  and only once steps 1-5 are done).
- Step statuses: done = target met; in_progress = some progress (cash > 0, retirement saving > 0, debt below its opening balance);
  todo = none yet; not_applicable; unknown = the data cannot say (401(k) contributions).
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Literal

from pydantic import Field

from ..config import assumptions as load_assumptions
from ..data.models import INSTALLMENT_TYPES, Account, Base, Profile
from . import forecast as fc
from .health import paycheck_to_paycheck
from .registry import NoArgs, register

Status = Literal["done", "in_progress", "todo", "not_applicable", "unknown"]
DAYS_PER_MONTH = 30.4375
MAX_SIM_MONTHS = 600


def thresholds() -> dict[str, float]:
    """Every threshold, read from assumptions.json on each call (so a change there takes effect, and tests can patch this)."""
    return {k: v["value"] for k, v in load_assumptions().items()}


# ---- debts ---------------------------------------------------------------------------------------------------------------------

def is_debt(a: Account) -> bool:
    """A liability with a balance that costs interest. A pay-in-full card is not one (see the module docstring)."""
    return a.is_liability and a.balance > 0 and a.subtype != "pay_in_full"


def _liquid(profile: Profile) -> float:
    return float(sum(a.balance for a in profile.accounts if a.is_liquid))


def _surplus(profile: Profile) -> float:
    if profile.summary is None:
        raise ValueError(f"{profile.id}: needs derive.enrich() and a summary first")
    return float(profile.summary.monthly_surplus)


def classify(a: Account, T: dict, steps_1_5_done: bool) -> str | None:
    """Why (if at all) extra payments on this debt are recommended: high_apr, optional, mortgage_extra; None = pay the minimum only."""
    if not is_debt(a):
        return None
    if a.type == "mortgage" and not (a.rate > T["mortgage_extra_rate"] and steps_1_5_done):
        return None                                               # payoff rule 4: excluded by default
    if a.rate >= T["high_apr_threshold"]:
        return "mortgage_extra" if a.type == "mortgage" else "high_apr"
    if a.rate >= T["safe_savings_rate"]:
        return "mortgage_extra" if a.type == "mortgage" else "optional"
    return None                                                   # payoff rule 3: cash earns more than the debt costs


def _order_key(a: Account):
    # high-APR group first, highest APR first; equal APR: cards before installment loans (payoff rule 2)
    return (a.rate < thresholds()["high_apr_threshold"], -round(a.rate, 6), a.type != "credit_card", a.id)


# ---- 1. priority waterfall -----------------------------------------------------------------------------------------------------

class WaterfallStep(Base):
    step: int
    name: str
    status: Status
    target: float | None
    current: float | None
    gap: float | None
    monthly_suggested: float                 # this step's share of the monthly surplus
    reason_code: str
    unit: Literal["dollars", "dollars_per_month"] = "dollars"      # what target / current / gap measure (step 3: dollars still owed)
    detail: dict[str, Any] = Field(default_factory=dict)


class Waterfall(Base):
    profile_id: str
    surplus: float
    current_step: int | Literal["gap"] | None  # the first step still to do; "gap" when spending exceeds income; None when nothing is left to do
    first_open_step: int | None              # the first step still to do, ignoring "gap" (what current_step would be with a surplus)
    steps: list[WaterfallStep]
    unallocated: float                       # surplus left after every step that could take some


def _ira_monthly(profile: Profile) -> float:
    """Monthly saving into an IRA, observed over the history (the 'Transfer to IRA' flow; it equals the transaction-level total)."""
    return -sum(r.monthly_amount for r in profile.recurring if r.kind == "investment_transfer")


def _under_age(profile: Profile, T: dict) -> bool:
    return any(d.age < T["dependent_age_limit"] for d in profile.dependents)


@register("priority_waterfall", result=Waterfall, category="rules")
def priority_waterfall(profile: Profile) -> Waterfall:
    """Orders where each dollar of monthly surplus should go: starter fund, 401(k) match, high-APR debt, full cushion, retirement,
    education, then optional extras. The first step not done gets the surplus; any remainder flows on. An 'unknown' step never blocks."""
    T = thresholds()
    ess, liquid, surplus = fc.essential_monthly(profile), _liquid(profile), _surplus(profile)
    retired = profile.employment == "retired"
    steps: list[WaterfallStep] = []

    def add(step, name, status, target, current, gap, reason, unit="dollars", **detail):
        steps.append(WaterfallStep(step=step, name=name, status=status, target=_r(target), current=_r(current), gap=_r(gap),
                                   monthly_suggested=0.0, reason_code=reason, unit=unit, detail=detail))

    # 1 starter emergency fund
    target = min(T["starter_cushion"], ess)
    gap = max(0.0, target - liquid)
    add(1, "Starter emergency fund", "done" if gap == 0 else "in_progress" if liquid > 0 else "todo", target, liquid, gap, "no_buffer")

    # 2 employer match: the data has no 401(k) contribution information, so a match we know exists is 'unknown'
    match = profile.retirement.employer_match_pct
    if not retired and profile.retirement.has_401k and match > 0:
        add(2, "Employer 401(k) match", "unknown", match * profile.gross_monthly, None, None, "free_money", "dollars_per_month",
            employer_match_pct=match)
    else:
        add(2, "Employer 401(k) match", "not_applicable", None, None, None, "free_money")

    # 3 high-interest debt (not mortgages), highest APR first
    high = sorted((a for a in profile.accounts if is_debt(a) and a.type != "mortgage" and a.rate >= T["high_apr_threshold"]), key=_order_key)
    owed = float(sum(a.balance for a in high))
    paying_down = any(a.balance < a.opening_balance for a in high)
    add(3, "High-interest debt", "done" if not high else "in_progress" if paying_down else "todo", 0.0, owed, owed, "high_apr",
        accounts=[a.id for a in high])

    # 4 full emergency fund: 6 months when paycheck-to-paycheck or there are young dependents
    p2p = paycheck_to_paycheck(profile).flag
    young = _under_age(profile, T)
    months = T["cushion_months_high"] if p2p or young else T["cushion_months"]
    target = months * ess
    gap = max(0.0, target - liquid)
    invested = float(sum(a.balance for a in profile.accounts if a.type == "ira"))
    covered = retired and invested >= T["retiree_investment_cover_months"] * ess       # a retiree's drawdown backstop (see assumptions)
    add(4, "Full emergency fund", "done" if gap == 0 or covered else "in_progress" if liquid > 0 else "todo", target, liquid,
        0.0 if covered else gap, "cushion", months=months, paycheck_to_paycheck=p2p, dependents_under_limit=young,
        covered_by_investments=bool(covered and gap > 0), investments=_r(invested))

    # 5 retirement: toward retirement_target_pct of gross, IRA saving plus the employer match when known
    if retired:
        add(5, "Retirement saving", "not_applicable", None, None, None, "retirement_gap", "dollars_per_month")
    else:
        ira, match_known = _ira_monthly(profile), (match * profile.gross_monthly if profile.retirement.has_401k else 0.0)
        target = T["retirement_target_pct"] * profile.gross_monthly
        current = ira + match_known
        gap = max(0.0, target - current)
        add(5, "Retirement saving", "done" if gap == 0 else "in_progress" if current > 0 else "todo", target, current, gap,
            "retirement_gap", "dollars_per_month", ira_monthly=_r(ira), match_monthly=_r(match_known))

    # 6 education: only with young dependents, and only once steps 1-5 are done or in progress (retirement always comes first)
    early = steps[:5]
    blocked = [s.step for s in early if s.status == "todo"]
    if _under_age(profile, T):
        add(6, "Education saving", "todo", None, None, None, "education", active=not blocked, blocked_by=blocked)
    else:
        add(6, "Education saving", "not_applicable", None, None, None, "education")

    # 7 moderate-APR debt (extra payments optional); mortgages only above mortgage_extra_rate and once steps 1-5 are done
    done_1_5 = all(s.status in ("done", "not_applicable", "unknown") for s in early)
    moderate = sorted((a for a in profile.accounts if classify(a, T, done_1_5) in ("optional", "mortgage_extra")), key=_order_key)
    add(7, "Moderate-APR debt or extra investing", "todo" if moderate else "not_applicable", None,
        float(sum(a.balance for a in moderate)) if moderate else None, None, "optional", accounts=[a.id for a in moderate])

    # hand out the surplus: first step not done, remainder to the next
    remaining = max(surplus, 0.0)
    current_step = None
    for s in steps:
        eligible = s.status in ("todo", "in_progress") and s.detail.get("active", True)
        if not eligible:
            continue
        current_step = current_step or s.step
        take = remaining if s.gap is None else min(remaining, s.gap)
        s.monthly_suggested = _r(take)
        remaining -= take
    s7 = steps[6]
    if remaining > 0 and s7.status == "not_applicable":          # nothing is left to fund: the rest goes to extra investing (step 7)
        s7.status, s7.monthly_suggested = "todo", _r(remaining)
        s7.detail["kind"] = "extra_investing"
        current_step, remaining = current_step or 7, 0.0
    return Waterfall(profile_id=profile.id, surplus=_r(surplus), current_step="gap" if surplus < 0 else current_step,
                     first_open_step=current_step, steps=steps, unallocated=_r(remaining))


def _r(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


# ---- 2. payoff priority --------------------------------------------------------------------------------------------------------

class PayoffArgs(Base):
    lump_sum: float = Field(0.0, ge=0, description="A one-time payment from existing cash, capped by the cushion floor")
    extra_monthly: float | None = Field(None, ge=0, description="Extra per month on top of minimums; omit to use the monthly surplus, 0 means none")
    use_cd: bool = Field(False, description="Consider breaking a CD to fund debt payments")


class PayoffItem(Base):
    account_id: str
    apr: float
    balance: float
    reason_code: Literal["high_apr", "optional", "mortgage_extra"]


class PlanOutcome(Base):
    months: int | None                       # None: not cleared within 50 years at this payment level
    total_interest: float


class CDOption(Base):
    account_id: str
    balance: float
    rate: float
    remaining_months: int
    penalty: float                           # penalty_days x balance x rate / 365
    interest_saved: float                    # debt interest avoided over the CD's remaining term by paying the CD balance now
    interest_given_up: float                 # CD interest forgone over the same term
    net_benefit: float                       # interest_saved - penalty - interest_given_up
    recommend_break: bool                    # only if net_benefit > 0


class PayoffPriority(Base):
    profile_id: str
    order: list[PayoffItem]
    safe_lump_sum: float                     # liquid - cushion_months x essentials, never below 0
    safe_lump_sum_low_cushion: float | None  # the card exception (APR >= card_low_cushion_apr): down to low_cushion_months; else None
    lump_sum_applied: float                  # the requested lump sum, capped at the applicable floor
    extra_monthly_applied: float
    avalanche: PlanOutcome | None
    snowball: PlanOutcome | None
    snowball_extra_cost: float | None        # extra interest vs avalanche; only when there are snowball_min_count small debts
    cd: CDOption | None
    flags: list[str]


def simulate(debts: list[Account], order: list[Account], lump: float, extra: float, horizon: int | None = None) -> tuple[int | None, float]:
    """Month-by-month payoff: interest accrues, each debt gets its minimum, extra money goes down the `order`, and a paid-off debt's
    minimum rolls into the extra money (freed payments). Returns (months until all are paid or None, interest paid in the horizon)."""
    bal = {a.id: a.balance for a in debts}
    for a in order:                                               # the lump sum goes down the order at month 0
        pay = min(lump, bal[a.id])
        bal[a.id] -= pay
        lump -= pay
    interest, months = 0.0, 0
    limit = horizon if horizon is not None else MAX_SIM_MONTHS
    while months < limit and any(b > 0.005 for b in bal.values()):
        months += 1
        for a in debts:
            i = bal[a.id] * a.rate / 12
            bal[a.id] += i
            interest += i
        pool = extra
        for a in debts:
            pay = min(a.min_payment, bal[a.id])
            bal[a.id] -= pay
            pool += a.min_payment - pay                           # whatever the minimum did not need rolls over
        for a in order:
            if pool <= 0:
                break
            pay = min(pool, bal[a.id])
            bal[a.id] -= pay
            pool -= pay
    cleared = all(b <= 0.005 for b in bal.values())
    return (months if cleared else None), interest


@register("payoff_priority", args=PayoffArgs, result=PayoffPriority, category="rules")
def payoff_priority(profile: Profile, *, lump_sum: float = 0.0, extra_monthly: float | None = None, use_cd: bool = False) -> PayoffPriority:
    """Orders debts for extra payments, computes the safe lump sum, and raises flags.

    Highest APR first for debts at or above the high-APR threshold (cards before loans at equal APR); a lump sum never takes cash
    below the cushion floor (cards at 20%+ may go lower, flagged); mortgages and debts cheaper than savings earn are left alone."""
    T = thresholds()
    wf = priority_waterfall(profile)
    done_1_5 = all(s.status in ("done", "not_applicable", "unknown") for s in wf.steps[:5])
    ess, liquid, surplus = fc.essential_monthly(profile), _liquid(profile), _surplus(profile)
    flags: list[str] = []

    reasons = {a.id: classify(a, T, done_1_5) for a in profile.accounts}
    chosen = sorted((a for a in profile.accounts if reasons[a.id]), key=_order_key)
    order = [PayoffItem(account_id=a.id, apr=a.rate, balance=round(a.balance, 2), reason_code=reasons[a.id]) for a in chosen]

    # cushion floor for lump sums
    safe = max(0.0, liquid - T["cushion_months"] * ess)
    expensive_card = any(a.type == "credit_card" and a.rate >= T["card_low_cushion_apr"] for a in chosen)
    safe_low = max(0.0, liquid - T["low_cushion_months"] * ess) if expensive_card else None
    if expensive_card:
        flags.append("tradeoff_low_cushion")
    applied_lump = min(lump_sum, safe_low if safe_low is not None else safe)

    all_debts = [a for a in profile.accounts if is_debt(a)]
    if not all_debts:
        flags.append("no_debt")
    if surplus <= 0:
        flags.append("no_surplus")
    if any(a.rate < T["safe_savings_rate"] for a in all_debts):
        flags.append("keep_cash_earns_more")
    if any(a.subtype == "promotional" and a.maturity_date and
           0 <= (a.maturity_date - profile.as_of_date).days <= T["promo_expiry_days"] for a in all_debts):
        flags.append("promo_rate_expiring")
    if all_debts:
        flags.append("prepayment_penalty_assumed_none")

    extra = max(surplus, 0.0) if extra_monthly is None else extra_monthly
    avalanche = snowball = cost = cd = None
    if chosen:
        months, interest = simulate(chosen, chosen, applied_lump, extra)
        avalanche = PlanOutcome(months=months, total_interest=round(interest, 2))
        small = [a for a in chosen if a.balance < T["snowball_small_balance"]]
        if len(small) >= T["snowball_min_count"]:
            by_balance = sorted(chosen, key=lambda a: (a.balance, a.id))
            m2, i2 = simulate(chosen, by_balance, applied_lump, extra)
            snowball = PlanOutcome(months=m2, total_interest=round(i2, 2))
            cost = round(i2 - interest, 2)

    if use_cd:
        cds = [a for a in profile.accounts if a.type == "cd" and a.balance > 0]
        if cds:
            cd = _cd_option(cds[0], profile, chosen, applied_lump, extra)

    return PayoffPriority(profile_id=profile.id, order=order, safe_lump_sum=round(safe, 2),
                          safe_lump_sum_low_cushion=_r(safe_low), lump_sum_applied=round(applied_lump, 2),
                          extra_monthly_applied=round(extra, 2), avalanche=avalanche, snowball=snowball, snowball_extra_cost=cost,
                          cd=cd, flags=flags)


def _cd_option(cd: Account, profile: Profile, chosen: list[Account], lump: float, extra: float) -> CDOption:
    """Break the CD and pay debt with its balance? Net benefit = debt interest saved over the CD's remaining term, minus the
    early-withdrawal penalty, minus the CD interest given up. All three are reported."""
    months = cd.term_months_remaining
    if months is None:
        months = math.ceil(((cd.maturity_date - profile.as_of_date).days if cd.maturity_date else 0) / DAYS_PER_MONTH)
    months = max(int(months), 1)
    penalty = (cd.penalty_days or 0) * cd.balance * cd.rate / 365
    given_up = cd.balance * cd.rate * months / 12
    saved = 0.0
    if chosen:
        _, without = simulate(chosen, chosen, lump, extra, horizon=months)
        _, with_cd = simulate(chosen, chosen, lump + cd.balance, extra, horizon=months)
        saved = without - with_cd
    net = saved - penalty - given_up
    return CDOption(account_id=cd.id, balance=round(cd.balance, 2), rate=cd.rate, remaining_months=months, penalty=round(penalty, 2),
                    interest_saved=round(saved, 2), interest_given_up=round(given_up, 2), net_benefit=round(net, 2), recommend_break=net > 0)


# ---- 3. candidate moves --------------------------------------------------------------------------------------------------------

class CandidateMove(Base):
    move_code: str
    priority: int                            # the waterfall step it serves (1-7); 8 = an opportunity outside the waterfall
    addresses_current_step: bool
    facts: dict[str, Any]                    # what an explanation needs; the advisor turns these into placeholders


class CandidateMoves(Base):
    profile_id: str
    current_step: int | Literal["gap"] | None
    moves: list[CandidateMove]               # in waterfall order; the only moves the advisor may recommend


OPPORTUNITY = 8


@register("candidate_moves", result=CandidateMoves, category="rules")
def candidate_moves(profile: Profile) -> CandidateMoves:
    """Every move whose condition holds for this customer, in waterfall order. The advisor picks from this list and nothing else."""
    T = thresholds()
    wf = priority_waterfall(profile)
    step = {s.step: s for s in wf.steps}
    ess, liquid, surplus = fc.essential_monthly(profile), _liquid(profile), _surplus(profile)
    retired = profile.employment == "retired"
    moves: list[CandidateMove] = []

    def add(code: str, priority: int, **facts):
        moves.append(CandidateMove(move_code=code, priority=priority, addresses_current_step=priority == wf.current_step and wf.current_step != 0, facts=facts))

    open_ = lambda n: step[n].status in ("todo", "in_progress")
    accounts = lambda ids: [{"account_id": a.id, "apr": a.rate, "balance": round(a.balance, 2)} for a in profile.accounts if a.id in ids]
    if surplus < 0:
        add("close_monthly_gap", 0, monthly_gap=_r(-surplus), essentials_monthly=_r(ess))
        moves[-1].addresses_current_step = True                    # with no surplus, this is the move that matters now
    if open_(1):
        add("build_starter_cushion", 1, liquid=_r(liquid), target=step[1].target, gap=step[1].gap)
    if step[2].status == "unknown":
        add("confirm_401k_match", 2, employer_match_pct=step[2].detail["employer_match_pct"], monthly_to_get_match=step[2].target)
    cards = [a for a in profile.accounts if is_debt(a) and a.type == "credit_card" and a.rate >= T["high_apr_threshold"]]
    if cards:
        add("pay_card_high_apr", 3, accounts=accounts({a.id for a in cards}), highest_apr=max(a.rate for a in cards),
            total_balance=_r(sum(a.balance for a in cards)))
    loans = [a for a in profile.accounts if is_debt(a) and a.type in INSTALLMENT_TYPES and a.type != "mortgage" and a.rate >= T["high_apr_threshold"]]
    if loans:
        add("pay_loan_high_apr", 3, accounts=accounts({a.id for a in loans}), highest_apr=max(a.rate for a in loans),
            total_balance=_r(sum(a.balance for a in loans)))
    if open_(4):
        add("build_full_cushion", 4, liquid=_r(liquid), target=step[4].target, gap=step[4].gap, months=step[4].detail["months"])
    if open_(5) and not retired:
        add("increase_retirement", 5, current_monthly=step[5].current, target_monthly=step[5].target, gap_monthly=step[5].gap)
    if step[6].status == "todo" and step[6].detail.get("active"):
        add("start_education_savings", 6, dependents_under_limit=sum(d.age < T["dependent_age_limit"] for d in profile.dependents))
    if profile.credit_score < T["credit_building_score"] and not any(a.type == "credit_card" for a in profile.accounts):
        add("start_credit_building", OPPORTUNITY, credit_score=profile.credit_score)
    checking = float(sum(a.balance for a in profile.accounts if a.type == "checking"))
    safe_lump = max(0.0, liquid - T["cushion_months"] * ess)
    cash_goes_to_debt = open_(3) and safe_lump > 0                 # idle cash should pay the expensive debt, not sit in savings
    if not cash_goes_to_debt and (liquid > T["idle_cash_months"] * ess or checking > T["idle_checking_months"] * ess):
        add("move_idle_cash_to_hys", OPPORTUNITY, liquid=_r(liquid), checking=_r(checking), essentials_monthly=_r(ess),
            safe_savings_rate=T["safe_savings_rate"])
    window = timedelta(days=T["cd_review_months"] * DAYS_PER_MONTH)
    for cd in (a for a in profile.accounts if a.type == "cd" and a.maturity_date):
        if timedelta(0) <= cd.maturity_date - profile.as_of_date <= window:
            add("cd_renewal_review", OPPORTUNITY, account_id=cd.id, balance=_r(cd.balance), rate=cd.rate, matures=cd.maturity_date.isoformat())
    extra_days = T["extra_paycheck_days"]
    events = fc.forecast_liquid(profile, horizon_months=math.ceil(extra_days / DAYS_PER_MONTH) + 1, paths=100, with_backtest=False).events
    soon = [e for e in events if e.kind == "extra_paycheck" and 0 <= (e.day - profile.as_of_date).days <= extra_days]
    if soon:
        add("use_extra_paycheck", OPPORTUNITY, date=soon[0].day.isoformat())
    freed = _freed_payments(profile, T)
    if freed:
        add("recently_freed_payment", OPPORTUNITY, freed=freed)
    saved = -sum(r.monthly_amount for r in profile.recurring if r.kind == "savings_transfer" and r.cadence != "irregular")
    if surplus > 0 and saved < T["automate_coverage_min"] * surplus:
        add("automate_savings", OPPORTUNITY, surplus=_r(surplus), automated_monthly=_r(max(saved, 0.0)), coverage=round(max(saved, 0.0) / surplus, 2))
    moves.sort(key=lambda m: m.priority)                          # stable: ties keep the order above
    return CandidateMoves(profile_id=profile.id, current_step=wf.current_step, moves=moves)


def _freed_payments(profile: Profile, T: dict) -> list[dict]:
    """Debts closed within the last freed_payment_days, and loans ending within the next freed_payment_days."""
    days, out = T["freed_payment_days"], []
    for a in profile.accounts:
        if a.is_liability and a.type != "credit_card" and a.maturity_date and -days <= (a.maturity_date - profile.as_of_date).days <= days:
            out.append({"account_id": a.id, "ends": a.maturity_date.isoformat(), "state": "ended" if a.maturity_date <= profile.as_of_date else "ending"})
    for r in ([] if out else profile.ended_recurring):          # a payment that stopped; skipped when the closed account itself was found
        if r.kind in ("loan_payment", "card_payment") and 0 <= (profile.as_of_date - r.last_date).days <= days:
            out.append({"account_id": r.account_id, "ends": r.last_date.isoformat(), "state": "ended"})
    return out


# ---- 4. verdict ----------------------------------------------------------------------------------------------------------------

Verdict = Literal["on_track", "plan_ahead", "act_now"]


def verdict(profile: Profile, overall_status: str, overdraft_probability: float = 0.0, wf: Waterfall | None = None) -> Verdict:
    """The advisor's verdict, decided in code (docs/rules.md section 4). act_now: the first open step is 1 or 3, or overall_status is
    at_risk. plan_ahead: it is 4-6, or overall_status is watch. Otherwise on_track.
    Negative surplus raises the floor to plan_ahead, and to act_now when an overdraft is possible or the customer's investments
    cover fewer than retiree_investment_cover_months of essentials (no backstop for the gap)."""
    T = thresholds()
    wf = wf or priority_waterfall(profile)
    step = wf.first_open_step
    if step in (1, 3) or overall_status == "at_risk":
        out: Verdict = "act_now"
    elif step in (4, 5, 6) or overall_status == "watch":
        out = "plan_ahead"
    else:
        out = "on_track"
    if wf.surplus < 0 and out != "act_now":
        invested = float(sum(a.balance for a in profile.accounts if a.type == "ira"))
        no_backstop = invested < T["retiree_investment_cover_months"] * fc.essential_monthly(profile)
        out = "act_now" if overdraft_probability > 0 or no_backstop else "plan_ahead"
    return out

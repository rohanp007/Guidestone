"""Debts in scope for a payoff plan, their minimum payments, and the month-by-month amortization engine.

Pure arithmetic: no forecasting, no randomness. Dates always count from AS_OF_DATE (config), never from date.today().

Conventions (docs/rules.md section 2 is the spec for which debts count):
- A debt is a liability with a balance, except a pay-in-full card (rules.is_debt); mortgages only with include_mortgage=True.
- Loan minimum: the recurring loan payment observed in the ledger; if there is none, the amortized payment for the remaining term;
  as a last resort the account's own min_payment. Card minimum: max(card_min_floor, card_min_pct x balance + one month of interest),
  recomputed on the current balance every month (so a card's minimum shrinks as it is paid down).
- Cents: payments are whole cents in real life, so a payment that is a cent short of the exact amortizing payment leaves a few cents
  at the end. A balance below the compounded rounding of that many payments (at most $1) counts as paid, so $222.44/mo on $10,000 at
  12% is 60 months, not 61. A genuine shortfall (a dime or more over two payments) is not forgiven.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from ..config import AS_OF_DATE, assumptions as load_assumptions
from ..data.models import Base, Profile
from . import forecast as fc
from .rules import is_debt

MAX_TOLERANCE = 1.0              # largest balance (dollars) that counts as paid because of cent-rounding


class Debt(Base):
    id: str
    type: str = "personal_loan"
    kind: Literal["card", "loan"] = "loan"
    apr: float
    balance: float
    fixed_min: float | None = None            # loans: a fixed monthly payment
    card_min_pct: float | None = None         # cards: share of the balance in the minimum
    card_min_floor: float | None = None       # cards: the minimum never falls below this
    min_source: str = "given"                  # observed, amortized, account, formula, given
    term_months_remaining: int | None = None

    def minimum(self, balance: float) -> float:
        """The minimum due this month on `balance` (the balance before this month's interest)."""
        if self.fixed_min is not None:
            return self.fixed_min
        return max(self.card_min_floor or 0.0, (self.card_min_pct or 0.0) * balance + balance * self.apr / 12)


def amortized_payment(principal: float, apr: float, months: int) -> float:
    """Level monthly payment that clears `principal` in `months` payments at `apr`."""
    r = apr / 12
    if months <= 0:
        return principal
    return principal / months if r == 0 else principal * r / (1 - (1 + r) ** -months)


def debts_in_scope(profile: Profile, include_mortgage: bool = False) -> list[Debt]:
    """Every debt a payoff plan covers, in account order."""
    T = {k: v["value"] for k, v in load_assumptions().items()}
    out: list[Debt] = []
    for a in profile.accounts:
        if not is_debt(a) or (a.type == "mortgage" and not include_mortgage):
            continue
        if a.type == "credit_card":
            out.append(Debt(id=a.id, type=a.type, kind="card", apr=a.rate, balance=a.balance, card_min_pct=T["card_min_pct"],
                            card_min_floor=T["card_min_floor"], min_source="formula"))
            continue
        observed = [abs(r.monthly_amount) for r in profile.recurring
                    if r.kind == "loan_payment" and (acct := fc.debt_account(profile, r)) is not None and acct.id == a.id]
        if observed:
            payment, source = observed[0], "observed"
        elif a.term_months_remaining:
            payment, source = amortized_payment(a.balance, a.rate, a.term_months_remaining), "amortized"
        else:
            payment, source = a.min_payment, "account"
        out.append(Debt(id=a.id, type=a.type, kind="loan", apr=a.rate, balance=a.balance, fixed_min=payment, min_source=source,
                        term_months_remaining=a.term_months_remaining))
    return out


def excluded_mortgages(profile: Profile, include_mortgage: bool) -> list[str]:
    """Mortgages with a balance that a plan left out (so a caller can say so)."""
    return [] if include_mortgage else [a.id for a in profile.accounts if is_debt(a) and a.type == "mortgage"]


# ---------------------------------------------------------------------------------------------------------------------------------

class DebtPayoff(Base):
    payoff_month: int | None                  # None: not cleared
    payoff_date: date | None
    interest: float
    paid: float
    never_pays_off: bool = False              # still owed at the end, and its payment did not even cover its interest


class ScheduleRow(Base):
    month: int                                # 0 = the lump sum, applied before any interest
    date: date
    payments: dict[str, float]
    interest: dict[str, float]
    balances: dict[str, float]                # after this month's payments
    total_payment: float


class Amortization(Base):
    months_to_payoff: int | None              # None: not cleared (see never_pays_off / hit_max_months)
    payoff_date: date | None
    total_interest: float
    total_paid: float
    never_pays_off: bool                      # some debt's payment did not cover its interest, with no extra reaching it
    hit_max_months: bool                      # still owed after max_months, though payments do cover the interest
    max_months: int
    extra_monthly: float
    lump_sum: float                           # requested
    lump_applied: float                       # what the debts could take (never more than they owe)
    rollover: bool
    order: list[str]
    lump_order: list[str]
    per_debt: dict[str, DebtPayoff]
    schedule: list[ScheduleRow]


def _add_months(n: int) -> date:
    return fc.add_months(AS_OF_DATE, n)


def _tolerance(months: int, apr: float) -> float:
    r = apr / 12
    factor = months if r == 0 else ((1 + r) ** months - 1) / r              # what a payment error of one cent compounds to
    return min(MAX_TOLERANCE, 0.005 * max(factor, 1.0))


def amortize(debts: list[Debt], extra_monthly: float, order: list[str], lump_sum: float = 0.0, lump_order: list[str] | None = None,
             rollover: bool = True, max_months: int | None = None) -> Amortization:
    """Month-by-month payoff. Each month: interest = balance x apr / 12; every debt gets its minimum (never more than it owes); the
    extra money goes to the first unpaid debt in `order`, and what that debt cannot take spills to the next. With `rollover`, a paid-off
    debt's minimum joins the extra money from the next month (and the unused part of its last minimum, that month). The lump sum is
    applied at month 0, down `lump_order` (default: `order`), before any interest.

    Floats inside, cents in the output."""
    if max_months is None:
        max_months = int(load_assumptions()["payoff_max_months"]["value"])
    by_id = {d.id: d for d in debts}
    if len(by_id) != len(debts):
        raise ValueError("duplicate debt ids")
    for name, ids in (("order", order), ("lump_order", lump_order or [])):
        bad = [i for i in ids if i not in by_id]
        if bad:
            raise ValueError(f"{name} has ids that are not in-scope debts: {bad}")
    rank = {i: n for n, i in enumerate(order)}
    order = list(order) + [d.id for d in debts if d.id not in rank]          # debts the caller left out go last
    lump_order = list(lump_order) if lump_order else order
    lump_order = lump_order + [i for i in order if i not in lump_order]

    bal = {d.id: d.balance for d in debts}
    paid_off: dict[str, int] = {}
    interest_by = {d.id: 0.0 for d in debts}
    paid_by = {d.id: 0.0 for d in debts}
    schedule: list[ScheduleRow] = []
    done = lambda i, m, tol=None: bal[i] <= (0.005 if tol is None else tol)

    lump_left, lump_applied = max(lump_sum, 0.0), 0.0
    lump_pay = {i: 0.0 for i in bal}
    for i in lump_order:
        take = min(lump_left, bal[i])
        bal[i] -= take
        lump_left -= take
        lump_pay[i] = take
        lump_applied += take
    for i in bal:
        paid_by[i] += lump_pay[i]
        if done(i, 0):
            bal[i] = 0.0
            paid_off[i] = 0
    if lump_applied > 0:
        schedule.append(ScheduleRow(month=0, date=_add_months(0), payments={i: _c(v) for i, v in lump_pay.items()},
                                    interest={i: 0.0 for i in bal}, balances={i: _c(v) for i, v in bal.items()}, total_payment=_c(lump_applied)))

    rolled = 0.0                                          # minimums of debts paid off in earlier months
    last_pay: dict[str, tuple[float, float]] = {}         # id -> (payment, interest) in its latest month
    months = 0
    while months < max_months and any(i not in paid_off for i in bal):
        months += 1
        pay, intr, begin = {}, {}, {}
        pool = max(extra_monthly, 0.0) + (rolled if rollover else 0.0)
        freed_now: list[float] = []
        for d in debts:
            if d.id in paid_off:
                continue
            start = begin[d.id] = bal[d.id]
            i_amt = start * d.apr / 12
            owed = start + i_amt
            due = d.minimum(start)
            p = min(due, owed)
            pool += (due - p) if rollover else 0.0                       # the part of the last minimum it did not need
            bal[d.id] = owed - p
            intr[d.id], pay[d.id] = i_amt, p
            interest_by[d.id] += i_amt
        for i in order:                                                  # extra money, first unpaid debt first
            if pool <= 0:
                break
            if i in paid_off or i not in pay:
                continue
            take = min(pool, bal[i])
            bal[i] -= take
            pay[i] += take
            pool -= take
        for d in debts:
            i = d.id
            if i not in pay:
                continue
            paid_by[i] += pay[i]
            last_pay[i] = (pay[i], intr[i])
            if done(i, months, _tolerance(months, d.apr)):
                bal[i] = 0.0
                paid_off[i] = months
                if rollover:
                    freed_now.append(d.minimum(begin[i]))
        rolled += sum(freed_now)
        schedule.append(ScheduleRow(month=months, date=_add_months(months), payments={i: _c(v) for i, v in pay.items()},
                                    interest={i: _c(v) for i, v in intr.items()}, balances={i: _c(bal[i]) for i in bal},
                                    total_payment=_c(sum(pay.values()))))

    cleared = all(i in paid_off for i in bal)
    stuck = {i for i in bal if i not in paid_off and last_pay.get(i, (0.0, 0.0))[0] <= last_pay.get(i, (0.0, 0.0))[1] + 1e-9}
    never = bool(stuck) and not cleared
    per_debt = {i: DebtPayoff(payoff_month=paid_off.get(i), payoff_date=_add_months(paid_off[i]) if i in paid_off else None,
                              interest=_c(interest_by[i]), paid=_c(paid_by[i]), never_pays_off=i in stuck) for i in bal}
    total_months = max(paid_off.values(), default=0) if cleared else None
    return Amortization(months_to_payoff=total_months, payoff_date=_add_months(total_months) if total_months is not None else None,
                        total_interest=_c(sum(interest_by.values())), total_paid=_c(sum(paid_by.values())), never_pays_off=never,
                        hit_max_months=not cleared and not never, max_months=max_months, extra_monthly=_c(extra_monthly),
                        lump_sum=_c(lump_sum), lump_applied=_c(lump_applied), rollover=rollover, order=order, lump_order=lump_order,
                        per_debt=per_debt, schedule=schedule)


def _c(x: float) -> float:
    return round(float(x) + 0.0, 2)

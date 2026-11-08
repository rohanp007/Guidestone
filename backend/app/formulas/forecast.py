"""Liquid-cash forecast: a deterministic schedule of dated cash events plus stochastic variable spend, run as Monte Carlo paths.

    schedule (recurring flows on real business days, raises, loans ending)
  + variable spend (bootstrap from the customer's own deseasonalized monthly samples x seasonal index x inflation)
  + savings interest at month end
  = daily liquid-cash paths (checking + savings), NOT clamped at zero: the point is to show shortfalls.

The p10-p90 bands and the overdraft / cushion risk probabilities are calibrated: deviations from the median path are scaled by
factors learned from backtest errors (scripts/fit_calibration.py, backend/app/calibration.json), because the raw model is over-confident.

Registered: forecast_liquid (full, for the API and charts) and forecast_liquid_summary (no daily / monthly arrays, for agents).
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import pickle
from dataclasses import dataclass, field, replace
from collections.abc import Sequence
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import ASSUMPTIONS_PATH, DATA_DIR, assumptions as load_assumptions
from ..data import derive, loader
from ..data.models import (Account, Assumption, Backtest, Base, ForecastChange, ForecastEvent, LiquidForecast, LowPoint, MonthEndPoint,
                           MonthEndProbability, MonthlyNet, PercentileBands, Profile, Recurring, Shortfall)
from pydantic import Field

from .registry import register

DEFAULT_HORIZON_MONTHS = 24
DAILY_DAYS = 90                  # `daily` covers days 0..90
BACKTEST_TRAIN_MONTHS = 18
BOOTSTRAP_BLOCKS = (2, 3)        # variable spend is resampled in runs of 2-3 consecutive months, keeping some real clustering
MAPE_FLOOR_SHARE = 0.10          # percentage errors are relative to max(|actual|, this share of monthly take-home): balances sit near zero
CACHE_PATH = DATA_DIR / ".cache" / "backtest.pkl"
# Model switches, so each source of variance can be measured on its own (scripts/measure_coverage.py). All on in production.
OPTIONS = {"total_spend": True, "uncertainty": True, "recurring_noise": True, "seasonal_uncertainty": True, "sweeps": True, "calibrate": True,
           "utility_seasonality": True}
DRIFT_REL_ANNUAL_SD = 0.006      # random-walk drift in the spending level, sd per year, relative. Measured: block-mean changes of the 100
                                 # customers' spending match independent monthly noise at 3-6 months and exceed it only slightly at 12 (~0.6%/yr)
CARD_MIN_PAYMENT, CARD_MIN_SHARE = 35.0, 0.02
PAYDOWN_RANGE = (-0.02, 0.10)    # monthly share of the carried card balance paid down, clipped
ESSENTIAL_RECURRING = frozenset({"housing", "utility", "insurance", "loan_payment"})
ESSENTIAL_VARIABLE = ("groceries", "healthcare", "transport")

# Recurring.kind -> forecast kind. savings_transfer is checking<->savings, liquid-neutral, so it is skipped.
_KIND = {"income": "income", "housing": "bill", "utility": "bill", "insurance": "bill", "subscription": "bill",
         "loan_payment": "debt_payment", "card_payment": "card_payment", "investment_transfer": "investment_transfer"}
_LOAN_TYPE = {"Mortgage": "mortgage", "Auto Loan": "auto_loan", "Personal Loan": "personal_loan"}


# ------------------------------------------------------------------ dates (same weekday-only rule as the generator's due())
def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def prev_bd(d: date) -> date:
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d


def next_bd(d: date) -> date:
    while d.weekday() >= 5: d += timedelta(days=1)
    return d


def _seed(*parts) -> int:
    """Stable across runs and machines (Python's hash() is not)."""
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:4], "big")


@dataclass(frozen=True)
class Flow:
    """A dated, deterministic cash flow added to the schedule (signed: negative leaves the account). Passed in `changes` next to
    ForecastChange. `replaces` names a debt account: its existing recurring payment (loan_payment / card_payment) is removed from
    the schedule, so the flows that replace it are never counted on top of it. A card that is replaced stops being carried by the
    card model: only its new purchases are paid, and the old balance is paid by the flows."""
    day: date
    amount: float
    description: str = ""
    kind: str = "scenario"
    replaces: str | None = None


def debt_account(profile: Profile, r: Recurring) -> Account | None:
    """The liability a recurring loan / card payment services (same matching the schedule has always used for loans)."""
    if r.kind == "card_payment":
        return next((a for a in profile.accounts if a.type == "credit_card"), None)
    t = _LOAN_TYPE.get(next((k for k in _LOAN_TYPE if k in r.description), ""), "")
    return next((a for a in profile.accounts if a.type == t), None)


# ------------------------------------------------------------------ 1. the deterministic schedule
@dataclass
class Schedule:
    as_of: date
    end: date
    month_ends: list[date]
    cash: list[list] = field(default_factory=list)          # [date, kind, signed amount, label, residuals or None]
    events: list[ForecastEvent] = field(default_factory=list)
    card_dates: list[date] = field(default_factory=list)    # card payment dates (the card is simulated, not scheduled, when total_spend is on)

    @property
    def days(self) -> int:
        return (self.end - self.as_of).days

    def vector(self) -> np.ndarray:
        """Net scheduled cash per day; index 0 is as_of and always 0."""
        v = np.zeros(self.days + 1)
        for e in self.cash:
            v[(e[0] - self.as_of).days] += e[2]
        return v


def _by_month(dates: list[date]) -> dict[tuple[int, int], list[date]]:
    out: dict[tuple[int, int], list[date]] = {}
    for d in sorted(dates):
        out.setdefault((d.year, d.month), []).append(d)
    return out


def _anniversaries(d: date, as_of: date) -> int:
    return (d.year - as_of.year) - ((d.month, d.day) < (as_of.month, as_of.day))


def _occurrences(r: Recurring, as_of: date, end: date, is_income: bool) -> list[date]:
    """Dates of one flow inside (as_of, end]. Paychecks move to the previous business day, everything else to the next."""
    adjust = prev_bd if is_income else next_bd
    out = []
    if r.cadence == "biweekly":
        d = r.last_date + timedelta(days=14)
        while d <= end + timedelta(days=7):
            out.append(adjust(d)); d += timedelta(days=14)
    else:
        k = 0
        while True:
            ms = add_months(date(as_of.year, as_of.month, 1), k)
            if ms > end: break
            dim = calendar.monthrange(ms.year, ms.month)[1]
            days = (15, dim) if (r.cadence == "semimonthly" and is_income) else (min(r.typical_day, dim),)
            out += [adjust(date(ms.year, ms.month, d)) for d in days]
            k += 1
    return [d for d in out if as_of < d <= end]


def build_schedule(profile: Profile, horizon_months: int, A: dict[str, float], changes: Sequence[ForecastChange | Flow] | None = None,
                   simulate_card: bool = False) -> Schedule:
    as_of = profile.as_of_date
    end = month_end(add_months(as_of, horizon_months))
    sch = Schedule(as_of, end, [month_end(add_months(as_of, k)) for k in range(1, horizon_months + 1)])
    infl = A["inflation_annual"]
    growth = A["ss_cola_annual"] if profile.employment == "retired" else A["raise_annual"]
    bump = A["rent_renewal_bump"]
    months_since = lambda d: (d.year - as_of.year) * 12 + d.month - as_of.month

    replaced = {c.replaces for c in changes or [] if isinstance(c, Flow) and c.replaces}     # debts whose payments the flows take over
    income_monthly = rent_monthly = 0.0
    for r in profile.recurring:
        kind = _KIND.get(r.kind)
        if kind is None:
            continue
        is_income, is_rent = kind == "income", r.description.startswith("Rent")
        dates = _occurrences(r, as_of, end, is_income)
        if kind == "card_payment" and simulate_card:
            sch.card_dates = dates                              # the payment depends on simulated card spending: see _simulate
            continue
        if replaced and kind in ("debt_payment", "card_payment") and getattr(debt_account(profile, r), "id", None) in replaced:
            continue                                            # replaced by Flow(s): drop the recurring payment, its events and its end
        per_event = r.monthly_amount / (2.0 if (r.cadence == "semimonthly" and is_income) else r.occurrences_per_month if r.cadence == "biweekly" else 1.0)

        if kind == "debt_payment":
            acct = debt_account(profile, r)
            if acct is not None and acct.term_months_remaining is not None:
                left = max(1, acct.term_months_remaining)
                if left < len(dates):
                    dates = dates[:left]
                    sch.events.append(ForecastEvent(day=dates[-1], kind="loan_ends", description=f"{r.description} ends",
                                                    monthly_impact=-r.monthly_amount))
        if is_income: income_monthly += r.monthly_amount
        if is_rent: rent_monthly += -r.monthly_amount

        def amount_on(d: date) -> float:
            n = _anniversaries(d, as_of)
            season = r.month_factors[d.month - 1] if r.month_factors and OPTIONS["utility_seasonality"] else 1.0
            return per_event * season * ((1 + growth) ** n if is_income else (1 + bump) ** n if is_rent
                                         else (1 + infl) ** (months_since(d) / 12) if kind in ("bill", "card_payment") else 1.0)

        for ds in _by_month(dates).values():
            if is_income and r.cadence == "biweekly" and len(ds) == 3:
                amt = amount_on(ds[2])
                sch.events.append(ForecastEvent(day=ds[2], kind="extra_paycheck", monthly_impact=amt,
                                                description=f"{ds[2]:%B} has a third paycheck (${amt:,.0f})"))
            elif not is_income and r.cadence == "monthly" and kind in ("bill", "debt_payment") and len(ds) == 2:
                amt = amount_on(ds[1])
                sch.events.append(ForecastEvent(day=ds[1], kind="double_payment", monthly_impact=amt,
                                                description=f"{r.description} lands twice in {ds[1]:%B} ({ds[0]:%b} {ds[0].day} and {ds[1]:%b} {ds[1].day}); extra payment ${abs(amt):,.0f}"))

        for d in dates:
            sch.cash.append([d, kind, amount_on(d), r.description, r.residuals or None])

    for n in range(1, horizon_months // 12 + 1):
        d = add_months(as_of, 12 * n)
        if d > end: break
        if income_monthly:
            sch.events.append(ForecastEvent(day=d, kind="raise", description=f"Annual {'COLA' if profile.employment == 'retired' else 'raise'} of {growth:.1%}",
                                            monthly_impact=income_monthly * (1 + growth) ** (n - 1) * growth, assumed_timing=True))
        if rent_monthly:
            sch.events.append(ForecastEvent(day=d, kind="rent_renewal", description=f"Rent renewal, +{bump:.1%}",
                                            monthly_impact=-rent_monthly * (1 + bump) ** (n - 1) * bump, assumed_timing=True))

    for a in profile.accounts_of("cd"):                       # auto-renews and stays outside liquid cash: a marker, no cash
        m = a.maturity_date
        while m and a.term_months and m <= end:
            if m > as_of:
                sch.events.append(ForecastEvent(day=m, kind="cd_matures", description="CD matures and auto-renews", monthly_impact=None))
            m = add_months(m, a.term_months)

    for c in changes or []:
        if isinstance(c, Flow):
            if as_of < c.day <= end:                            # flows are generated generically; those past the horizon are dropped
                sch.cash.append([c.day, c.kind, c.amount, c.description or c.kind, None])
            continue
        if c.type not in ("lump_sum", "income_change"):
            raise NotImplementedError(f"ForecastChange type {c.type!r} is not implemented yet")
        start = c.day or as_of + timedelta(days=1)
        if not as_of < start <= end:
            raise ValueError(f"{c.type} date {start} is outside the forecast window")
        if c.type == "lump_sum":
            sch.cash.append([start, "lump_sum", c.amount, c.description or "Lump sum", None])
            sch.events.append(ForecastEvent(day=start, kind="lump_sum", description=c.description or "Lump sum", monthly_impact=c.amount))
        else:
            pay = [e for e in sch.cash if e[1] == "income" and e[0] >= start]
            per_month: dict[tuple, int] = {}
            for e in pay: per_month[(e[0].year, e[0].month)] = per_month.get((e[0].year, e[0].month), 0) + 1
            for e in pay: e[2] += c.amount / per_month[(e[0].year, e[0].month)]
            sch.events.append(ForecastEvent(day=start, kind="income_change", description=c.description or "Income change", monthly_impact=c.amount))

    sch.events.sort(key=lambda e: (e.day, e.kind))
    return sch


def essential_monthly(profile: Profile) -> float:
    """One month of essentials: housing, utilities, insurance, loan payments, groceries, healthcare, transport.
    Leaves out discretionary categories, subscriptions and savings / investment transfers."""
    fixed = -sum(r.monthly_amount for r in profile.recurring if r.kind in ESSENTIAL_RECURRING)
    return fixed + sum(profile.variable_spend.get(c, 0.0) for c in ESSENTIAL_VARIABLE)


def _bootstrap_index(rng: np.random.Generator, n: int, paths: int, months: int) -> np.ndarray:
    """(paths, months) of indices into the customer's own history, drawn in blocks of 2-3 consecutive months so any real
    month-to-month clustering survives. Blocks wrap around the end of the history, so every month is equally likely and
    the average stays unbiased (otherwise the first and last months are under-weighted). Falls back to single months if
    the history is too short. The SAME indices pick the variable-spend draw and every noisy bill's residual, so
    their real co-movement (a cold month raises utilities and spending together) is kept."""
    longest = max(BOOTSTRAP_BLOCKS)
    if n < longest:
        return rng.integers(0, max(n, 1), (paths, months))
    blocks = -(-months // min(BOOTSTRAP_BLOCKS))
    lens = rng.choice(BOOTSTRAP_BLOCKS, size=(paths, blocks))
    starts = rng.integers(0, n, (paths, blocks))
    out, pos, rows = np.empty((paths, months), dtype=int), np.zeros(paths, dtype=int), np.arange(paths)
    for b in range(blocks):
        for j in range(longest):
            take = (j < lens[:, b]) & (pos < months)
            out[rows[take], pos[take]] = (starts[take, b] + j) % n
            pos[take] += 1
    return out


@dataclass
class Card:
    revolver: bool
    apr: float                 # decimal per year
    paydown: float             # share of the carried balance paid down each month
    share: float               # card purchases / all variable spend
    start_balance: float
    account_id: str = ""


def _card(profile: Profile) -> Card | None:
    """The card, if there is one and its payments were detected as recurring. The paydown rate is read off the balance
    trend: how fast the carried balance fell per month over the history."""
    acct = next((a for a in profile.accounts if a.type == "credit_card"), None)
    if acct is None or profile.spending is None or not any(r.kind == "card_payment" for r in profile.recurring):
        return None
    months = max(1, len(profile.history))
    avg = max(1.0, (acct.opening_balance + acct.balance) / 2)
    paydown = float(np.clip((acct.opening_balance - acct.balance) / (months * avg), *PAYDOWN_RANGE))
    return Card(profile.spending.carries_card_balance, acct.rate, paydown, profile.spending.card_share, acct.balance, acct.id)


# ------------------------------------------------------------------ 2-3. variable spend + paths
@dataclass
class Sim:
    schedule: Schedule
    pct: np.ndarray                      # (3, days + 1): p10, p50, p90 of liquid cash per day
    risks: dict[str, tuple[float, float, float, list[float]]]   # name -> (buffer, P(ever below), P(below within 90 days), P(below) at each month-end)
    net_per_month: np.ndarray            # (paths,)
    month1_variable: float
    month1_card: float | None            # median card payment in month 1, when the card is simulated
    month1_interest: float
    start_liquid: float
    paths: int


def _simulate(profile: Profile, horizon_months: int, paths: int, cushion: float, seed: int, A: dict[str, float],
              changes: Sequence[ForecastChange | Flow] | None, risk_factors: list[float] | None = None,
              income_scale: np.ndarray | None = None) -> Sim:
    """`risk_factors` are the calibration's month-end band factors. When given, the risk probabilities are computed on paths whose
    deviation from the median path is scaled by the factor interpolated for that day; the returned percentiles stay RAW (the
    calibration is fit on them, and _summarize scales the bands itself).

    `income_scale` is (paths, months): each path's paychecks are multiplied by its value for that month (the job-loss stress test).
    Everything else, including every random draw, is identical to the run without it, so the two runs are a paired comparison."""
    if profile.spending is None:
        raise ValueError(f"{profile.id}: needs derive.enrich() first")
    sp = profile.spending
    card = _card(profile) if OPTIONS["total_spend"] and sp.monthly_total_samples else None
    if card and any(isinstance(c, Flow) and c.replaces == card.account_id for c in changes or []):
        card = replace(card, revolver=False, start_balance=0.0)      # Flows pay the old balance: the card model pays new purchases only
    sch = build_schedule(profile, horizon_months, A, changes, simulate_card=card is not None)
    D, M = sch.days, len(sch.month_ends)
    rng = np.random.default_rng(seed)

    liquid = [a for a in profile.accounts if a.is_liquid]
    start = float(sum(a.balance for a in liquid))
    savings = next((a for a in liquid if a.type == "savings"), None)
    share = savings.balance / start if savings and start > 0 else 0.0
    rate = savings.rate if savings else 0.0

    # variable spend: block-bootstrapped months from the customer's own history. With a card, the draw is TOTAL spend
    # (checking + card); the checking part leaves daily and the card part leaves through the simulated card payment.
    samples = np.asarray(sp.monthly_total_samples if card else sp.monthly_variable_samples)
    index = derive.aggregate_seasonality(profile.variable_spend if card else profile.variable_spend_checking, sp.seasonality)
    calendar_month = np.array([me.month for me in sch.month_ends])
    seasonal = np.array([index[m] for m in calendar_month])[None, :]
    if OPTIONS["seasonal_uncertainty"]:
        # each path gets its own plausible seasonal pattern: a calendar month seen once is much less certain than one seen twice.
        # The same draw is used for the same calendar month in later years (the factor is a property of the customer, not the year).
        se = derive.aggregate_seasonality_se(profile.variable_spend if card else profile.variable_spend_checking, sp.seasonality_se)
        seasonal = np.maximum(seasonal + rng.normal(0, 1, (paths, 12))[:, calendar_month - 1] * np.array([se[m] for m in calendar_month]), 0.5)
    inflate = (1 + A["inflation_annual"]) ** (np.arange(1, M + 1) / 12)
    idx = _bootstrap_index(rng, len(samples), paths, M)
    draw = samples[idx] if len(samples) else np.zeros((paths, M))
    if OPTIONS["uncertainty"] and len(samples) > 1 and samples.mean() > 0:
        cv = samples.std(ddof=1) / samples.mean()
        level = 1 + rng.normal(0, cv / np.sqrt(len(samples)), (paths, 1))                       # how well the mean is known
        drift = np.cumsum(rng.normal(0, DRIFT_REL_ANNUAL_SD / np.sqrt(12), (paths, M)), axis=1)  # the level wanders slowly
        draw = draw * level * (1 + drift)
    monthly = draw * seasonal * inflate

    bounds = np.array([(me - sch.as_of).days for me in sch.month_ends])
    month_of_day = np.searchsorted(bounds, np.arange(1, D + 1), side="left")
    ndays = np.diff(np.concatenate([[0], bounds]))
    daily = monthly[:, month_of_day] / ndays[month_of_day]
    card_daily = card.share * daily if card else None
    net = sch.vector()[1:][None, :] - (daily - card_daily if card else daily)

    if income_scale is not None:
        pay = np.zeros(D)
        for e in sch.cash:
            if e[1] == "income":
                pay[(e[0] - sch.as_of).days - 1] += e[2]
        net += (income_scale[:, month_of_day] - 1.0) * pay[None, :]

    if OPTIONS["recurring_noise"]:                                    # bills whose amount really varies: bootstrap their own residuals
        for d, _, amount, _, resid in sch.cash:
            if resid is not None and len(resid) == len(samples):
                j = (d - sch.as_of).days - 1
                net[:, j] += amount * np.asarray(resid)[idx[:, month_of_day[j]]]

    month1_card = None
    if card:                                                          # track the card balance on every path
        cols = [(d - sch.as_of).days - 1 for d in sch.card_dates]
        cum = np.cumsum(card_daily, axis=1)
        carried, prev, pays = np.full(paths, card.start_balance), np.zeros(paths), []
        for c in cols:
            purchases, prev = cum[:, c] - prev, cum[:, c]
            interest = carried * card.apr / 12 if card.revolver else 0.0
            due = carried + purchases + interest
            if card.revolver:                                         # pay the cycle's new charges + interest + a slice of the old balance
                pay = np.clip(due - carried * (1 - card.paydown), np.minimum(due, np.maximum(CARD_MIN_PAYMENT, CARD_MIN_SHARE * due)), due)
            else:
                pay = due
            carried = due - pay
            net[:, c] -= pay
            pays.append(pay)
        in_month1 = [p for p, d in zip(pays, sch.card_dates) if d <= sch.month_ends[0]]
        month1_card = float(np.median(sum(in_month1))) if in_month1 else 0.0
        by_date = dict(zip(sch.card_dates, pays))
        for ds in _by_month(sch.card_dates).values():              # a payment that slid across the month boundary lands twice
            if len(ds) == 2:
                amt = -float(np.median(by_date[ds[1]]))
                sch.events.append(ForecastEvent(day=ds[1], kind="double_payment", monthly_impact=amt,
                                                description=f"Card payments land twice in {ds[1]:%B} ({ds[0]:%b} {ds[0].day} and {ds[1]:%b} {ds[1].day}); extra payment ${abs(amt):,.0f}"))
        sch.events.sort(key=lambda e: (e.day, e.kind))

    balance = np.empty((paths, D + 1))
    balance[:, 0] = start
    carry, interest1, a = np.full(paths, start), 0.0, 0
    rule = sp.sweep_rule if OPTIONS["sweeps"] else None
    for k in range(M):
        b = bounds[k]
        if rule and a < b:                                            # surplus leaves on the customer's usual sweep day
            me = sch.month_ends[k]
            sweep_day = (next_bd(date(me.year, me.month, min(rule.day, me.day))) - sch.as_of).days
            s_end = min(max(sweep_day, a + 1), b)
            head = carry[:, None] + np.cumsum(net[:, a:s_end], axis=1)
            head[:, -1] -= np.where(head[:, -1] > rule.threshold, head[:, -1] - rule.target, 0.0)
            block = head if s_end == b else np.hstack([head, head[:, -1:] + np.cumsum(net[:, s_end:b], axis=1)])
        else:
            block = carry[:, None] + np.cumsum(net[:, a:b], axis=1)
        interest = np.maximum(block[:, -1], 0.0) * share * rate / 12
        block[:, -1] += interest
        if k == 0: interest1 = float(np.median(interest))
        balance[:, a + 1:b + 1] = block
        carry, a = block[:, -1], b

    pct = np.percentile(balance, [10, 50, 90], axis=0)
    scored = balance
    if risk_factors is not None:
        scored = pct[1] + (balance - pct[1]) * band_factors(risk_factors, D)[None, :]
    risks = {}
    me_days = [(me - sch.as_of).days for me in sch.month_ends]
    for name, buffer in (("overdraft", 0.0), ("cushion", cushion)):
        below = scored < buffer
        risks[name] = (buffer, float(below.any(axis=1).mean()), float(below[:, :DAILY_DAYS + 1].any(axis=1).mean()),
                         [float(below[:, i].mean()) for i in me_days])
    checking_share = 1 - card.share if card else 1.0
    return Sim(sch, pct, risks, (balance[:, -1] - start) / M,
               float(np.median(monthly[:, 0])) * checking_share, month1_card, interest1, start, paths)


# ------------------------------------------------------------------ 4. summarize
def _last_below(curve: np.ndarray, buffer: float, as_of: date) -> date | None:
    hit = np.flatnonzero(curve < buffer)
    return as_of + timedelta(days=int(hit[-1])) if hit.size else None


def _first_below(curve: np.ndarray, buffer: float, as_of: date) -> date | None:
    hit = curve < buffer
    return as_of + timedelta(days=int(hit.argmax())) if hit.any() else None


def _used_assumptions(profile: Profile, A_all: dict[str, dict], sch: Schedule) -> dict[str, Assumption]:
    names = ["inflation_annual", "forecast_paths", "ss_cola_annual" if profile.employment == "retired" else "raise_annual"]
    if any(r.description.startswith("Rent") for r in profile.recurring): names.append("rent_renewal_bump")
    return {n: Assumption(**A_all[n]) for n in names}


def no_narrowing(p50: np.ndarray, p10: np.ndarray, p90: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bands that never get narrower as the horizon grows. The calibration factors are fit per horizon bucket and interpolated between
    anchors (day 30, 90, 180); the short-horizon factor is large (the raw band is tiny there), so a calibrated band could be wider at
    day 30 than at day 90. Uncertainty about the future cannot shrink with distance, so each day's width is at least the widest earlier
    width (both sides scaled alike, keeping the band's shape). This can only widen a band, so the calibrated coverage stays at or above
    what the backtest measured."""
    width = p90 - p10
    widest = np.maximum.accumulate(width)
    lower = np.divide(p50 - p10, width, out=np.full_like(width, np.nan), where=width > 1e-9)       # the share of the band below the median
    idx = np.maximum.accumulate(np.where(np.isnan(lower), 0, np.arange(len(lower))))                # forward-fill it across zero-width days
    lower = np.where(np.isnan(lower[idx]), 0.5, lower[idx])
    return p50 - widest * lower, p50 + widest * (1 - lower)


def _summarize(profile: Profile, sim: Sim, horizon_months: int, A_all: dict[str, dict], daily_days: int = DAILY_DAYS) -> LiquidForecast:
    sch, p10, p50, p90 = sim.schedule, *sim.pct
    cal = _calibration() if OPTIONS["calibrate"] else None
    p10d, p90d, p10m, p90m = p10, p90, p10, p90                # daily-chart bands and monthly-chart bands
    if cal:                                                    # scale the bands around the median by the horizon's factor
        fd, fm = band_factors(cal["daily_factors"], sch.days), band_factors(cal["month_end_factors"], sch.days)
        p10d, p90d = no_narrowing(p50, p50 - (p50 - p10) * fd, p50 + (p90 - p50) * fd)
        p10m, p90m = no_narrowing(p50, p50 - (p50 - p10) * fm, p50 + (p90 - p50) * fm)
    day = lambda i: sch.as_of + timedelta(days=int(i))
    risk = lambda name: Shortfall(buffer=sim.risks[name][0], probability=sim.risks[name][1], probability_90d=sim.risks[name][2],
                                  first_date_p50=_first_below(p50, sim.risks[name][0], sch.as_of),
                                  first_date_p10=_first_below(p10d, sim.risks[name][0], sch.as_of),
                                  share_of_days_below=float((p50[1:] < sim.risks[name][0]).mean()),
                                  last_date_below_p50=_last_below(p50, sim.risks[name][0], sch.as_of),
                                  month_end_probability_by_month=[MonthEndProbability(month=me.strftime("%Y-%m"), probability=pr)
                                                                  for me, pr in zip(sch.month_ends, sim.risks[name][3])])
    low = int(p50.argmin())
    me_idx = [(me - sch.as_of).days for me in sch.month_ends]

    month1 = [e for e in sch.cash if e[0] <= sch.month_ends[0]]
    drivers: dict[str, float] = {}
    for e in month1:
        drivers[e[1]] = drivers.get(e[1], 0.0) + e[2]
    if sim.month1_card is not None:
        drivers["card_payment"] = -sim.month1_card
    drivers["variable_spend_median"] = -sim.month1_variable
    drivers["interest"] = sim.month1_interest

    monthly = [MonthEndPoint(month=me.strftime("%Y-%m"), day=me, p10=float(p10m[i]), p50=float(p50[i]), p90=float(p90m[i]))
               for me, i in zip(sch.month_ends, me_idx)]
    return LiquidForecast(
        profile_id=profile.id, as_of_date=sch.as_of, horizon_months=horizon_months, paths=sim.paths, start_liquid=sim.start_liquid,
        end_balance=monthly[-1], balance_12m=monthly[11] if len(monthly) >= 12 else None,
        daily=PercentileBands(p10=p10d[:daily_days + 1].tolist(), p50=p50[:daily_days + 1].tolist(), p90=p90d[:daily_days + 1].tolist()),
        monthly=monthly,
        low_point=LowPoint(day=day(low), p50=float(p50[low]), p10=float(p10d[low])),
        overdraft_risk=risk("overdraft"), cushion_risk=risk("cushion"),
        monthly_net=MonthlyNet(mean=float(sim.net_per_month.mean()), p10=float(np.percentile(sim.net_per_month, 10)),
                               p90=float(np.percentile(sim.net_per_month, 90))),
        drivers=drivers, events=sch.events, assumptions=_used_assumptions(profile, A_all, sch),
        calibration=cal["note"] if cal else None,
    )


# ------------------------------------------------------------------ 5. backtest (cached per profile)
def _actual_liquid(profile: Profile, tx: pd.DataFrame) -> pd.Series:
    """Daily checking + savings balance rebuilt from the ledger."""
    start = pd.Timestamp(profile.history[0].month + "-01")
    days = pd.date_range(start, pd.Timestamp(profile.as_of_date))
    liq = tx[tx["account_type"].isin(["checking", "savings"])]
    daily = liq.groupby(liq["posted_date"].dt.normalize())["balance_change"].sum().reindex(days, fill_value=0.0)
    return sum(a.opening_balance for a in profile.accounts if a.is_liquid) + daily.cumsum()


def _backtest_inputs_hash() -> str:
    h = hashlib.sha256(loader.cache_key().encode())
    for f in (__file__, ASSUMPTIONS_PATH):          # the backtest depends on this code and on the assumptions it ran with
        st = os.stat(f)
        h.update(f"{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()


@dataclass
class BacktestCurves:
    """What the backtest forecast (raw, uncalibrated bands) and what actually happened, day by day after the cutoff."""
    cutoff: date
    actual: np.ndarray
    p10: np.ndarray
    p50: np.ndarray
    p90: np.ndarray
    outflow: float                       # average monthly checking outflow in the training months
    net_monthly: float
    known_one_off: str | None            # an event nobody could have forecast; such customers are left out of calibration and coverage

    @property
    def month_end_mask(self) -> np.ndarray:
        return np.array([d == month_end(d) for d in (self.cutoff + timedelta(days=i + 1) for i in range(len(self.actual)))])


def _backtest_setup(profile: Profile):
    """The profile as it stood after the first 18 months, ready to forecast the rest; None if there isn't enough history."""
    tx = loader.transactions_for(profile.id)
    statements = loader._tables()["statements"][profile.id]
    months = pd.PeriodIndex(pd.to_datetime(statements["month"]), freq="M").unique().sort_values()
    if len(months) <= BACKTEST_TRAIN_MONTHS:
        return None
    cutoff = months[BACKTEST_TRAIN_MONTHS - 1].end_time.date()
    cut, tx_cut, st_cut = derive.truncate(profile, tx, statements, cutoff)
    cut = derive.enrich(cut, tx_cut, st_cut)                  # the same derive functions, only on the first 18 months
    A_all = load_assumptions()
    return tx, cutoff, cut, st_cut, len(months) - BACKTEST_TRAIN_MONTHS, {k: v["value"] for k, v in A_all.items()}


def backtest_curves(profile: Profile, seed: int = 0) -> BacktestCurves | None:
    setup = _backtest_setup(profile)
    if setup is None:
        return None
    tx, cutoff, cut, st_cut, horizon, A = setup
    sim = _simulate(cut, horizon, int(A["forecast_paths"]), 0.0, _seed(profile.id, "backtest", seed), A, None)

    actual = _actual_liquid(profile, tx)
    start = actual.index.get_loc(pd.Timestamp(cutoff))
    n = min(len(sim.pct[1]) - 1, len(actual) - 1 - start)
    p10, p50, p90 = (c[1:n + 1] for c in sim.pct)
    outflow = float(st_cut[st_cut["account_type"] == "checking"]["balance_decreases"].mean())
    cd = bool(((tx["posted_date"] > pd.Timestamp(cutoff)) & (tx["description"] == "Transfer to CD")).any())
    return BacktestCurves(cutoff, actual.iloc[start + 1:][:n].to_numpy(), p10, p50, p90, outflow, profile.net_monthly,
                          "opened a CD in the test window" if cd else None)


@dataclass
class RiskOutcome:
    """One customer's backtest risk forecast against what happened in the test window."""
    start_liquid: float
    cushion: float
    p_overdraft: float                   # forecast probability of needing outside money at some point in the window
    p_cushion: float                     # ...of dropping below one month of essentials
    overdraft_happened: bool             # the ledger had to top checking up from an outside account (it never lets the balance go below $0)
    cushion_happened: bool
    known_one_off: str | None


def backtest_risks(profile: Profile, month_end_factors: list[float] | None = None, seed: int = 0) -> RiskOutcome | None:
    """Forecast the overdraft / cushion risk for the 6 test months from the first 18, with the paths scaled by `month_end_factors`
    (None = raw), and look up whether each event actually happened."""
    setup = _backtest_setup(profile)
    if setup is None:
        return None
    tx, cutoff, cut, st_cut, horizon, A = setup
    cushion = essential_monthly(cut)
    sim = _simulate(cut, horizon, int(A["forecast_paths"]), cushion, _seed(profile.id, "backtest", seed), A, None, month_end_factors)
    actual = _actual_liquid(profile, tx)
    window = actual.iloc[actual.index.get_loc(pd.Timestamp(cutoff)) + 1:].to_numpy()
    after = tx[tx["posted_date"] > pd.Timestamp(cutoff)]
    topped_up = bool(((after["account_type"] == "checking") & (after["category"] == "external_cash_management") & (after["balance_change"] > 0)).any())
    cd = bool((after["description"] == "Transfer to CD").any())
    return RiskOutcome(sim.start_liquid, cushion, sim.risks["overdraft"][1], sim.risks["cushion"][1], topped_up, bool((window < cushion).any()),
                       "opened a CD in the test window" if cd else None)


def _metrics(c: BacktestCurves) -> Backtest:
    a, p10, p50, p90, n = c.actual, c.p10, c.p50, c.p90, len(c.actual)
    err = np.abs(p50 - a) / np.maximum(np.abs(a), MAPE_FLOOR_SHARE * c.net_monthly)
    abs_err = np.abs(p50 - a)
    mape = lambda h: float(err[:h].mean()) if n >= h else None
    mean_err = lambda h: float(abs_err[:h].mean() / c.outflow) if n >= h and c.outflow > 0 else None
    # the forecast and the actual both start from the same balance, so the cumulative net change error is the endpoint error
    net_err = lambda h: float(abs_err[h - 1] / c.outflow) if n >= h and c.outflow > 0 else None
    me, inside = c.month_end_mask, (a >= p10) & (a <= p90)
    return Backtest(cutoff=c.cutoff, days=n, avg_monthly_outflow=c.outflow, known_one_off=c.known_one_off,
                    band_coverage_month_end=float(inside[me].mean()) if me.any() else None,
                    outflow_err_30d=mean_err(30), outflow_err_90d=mean_err(90), outflow_err_180d=mean_err(180),
                    net_change_err_30d=net_err(30), net_change_err_90d=net_err(90), net_change_err_180d=net_err(180),
                    mape_30d=mape(30), mape_90d=mape(90), mape_180d=mape(180), band_coverage=float(inside.mean()))


def run_backtest(profile: Profile, seed: int = 0) -> Backtest | None:
    curves = backtest_curves(profile, seed)
    return None if curves is None else _metrics(curves)


# ------------------------------------------------------------------ 5b. calibration (split-conformal, per horizon)
CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "calibration.json"
CAL_ANCHORS = (30, 90, 180)          # horizons (days) with their own band-scaling factor; linear in between, flat outside
CAL_BUCKETS = (45, 135)              # scores from days <=45 fit the 30-day factor, 46-135 the 90-day, later the 180-day
CAL_NOMINAL = 0.80                   # the p10-p90 band should contain the actual outcome 80% of the time
CAL_MIN_HALFWIDTH = 0.002            # band half-widths are floored at this share of a month's outflow when scoring (avoids dividing by ~0)


def _scores(c: BacktestCurves) -> np.ndarray:
    """How many band half-widths the actual outcome sits from the median, per day, on the side it fell: <= 1 means inside the band."""
    dev = c.actual - c.p50
    half = np.where(dev >= 0, c.p90 - c.p50, c.p50 - c.p10)
    return np.abs(dev) / np.maximum(half, CAL_MIN_HALFWIDTH * c.outflow)


def fit_calibration(curves: list[BacktestCurves], month_end_only: bool = False) -> list[float]:
    """One scaling factor per anchor horizon: the (finite-sample corrected) 80% quantile of the scores of that horizon's days,
    pooled across the given customers. `month_end_only` fits on month-end days only, for the monthly chart."""
    edges = [0, *CAL_BUCKETS, 10 ** 6]
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        parts = []
        for c in curves:
            day = np.arange(1, len(c.actual) + 1)
            take = (day > lo) & (day <= hi) & (c.month_end_mask if month_end_only else True)
            parts.append(_scores(c)[take])
        pooled = np.concatenate(parts)
        level = min(1.0, np.ceil((len(pooled) + 1) * CAL_NOMINAL) / len(pooled))
        out.append(float(np.quantile(pooled, level, method="higher")))
    return out


def band_factors(factors: list[float], days: int) -> np.ndarray:
    return np.interp(np.arange(days + 1), CAL_ANCHORS, factors)


def coverage_with(curves: list[BacktestCurves], daily: list[float] | None, month_end: list[float] | None) -> tuple[float, float]:
    """(month-end coverage, every-day coverage), pooled over customers. Month-end days use the month-end factors, the
    every-day figure uses the daily factors; None leaves the bands raw."""
    me_hits, me_n, hits, n = 0, 0, 0, 0
    for c in curves:
        days = len(c.actual)
        scores = _scores(c)
        fd = np.ones(days) if daily is None else band_factors(daily, days)[1:]
        fm = np.ones(days) if month_end is None else band_factors(month_end, days)[1:]
        me = c.month_end_mask
        me_hits, me_n = me_hits + (scores <= fm)[me].sum(), me_n + me.sum()
        hits, n = hits + (scores <= fd).sum(), n + days
    return me_hits / me_n, hits / n


def split_half_check(curves: list[BacktestCurves]) -> dict[str, float]:
    """Fit on one half of the customers, test on the other half (both ways); returns pooled held-out coverage."""
    a, b = curves[0::2], curves[1::2]
    fit = lambda cs: (fit_calibration(cs), fit_calibration(cs, month_end_only=True))
    (da, ma), (db, mb) = fit(a), fit(b)
    cal_b, cal_a = coverage_with(b, da, ma), coverage_with(a, db, mb)
    raw_b, raw_a = coverage_with(b, None, None), coverage_with(a, None, None)
    return dict(held_out_month_end=(cal_b[0] + cal_a[0]) / 2, held_out_all_days=(cal_b[1] + cal_a[1]) / 2,
                uncalibrated_month_end=(raw_b[0] + raw_a[0]) / 2, uncalibrated_all_days=(raw_b[1] + raw_a[1]) / 2)


@lru_cache(maxsize=1)
def _calibration() -> dict | None:
    try:
        return json.loads(CALIBRATION_PATH.read_text())
    except (OSError, ValueError):
        return None


_BACKTESTS: dict[str, Backtest | None] | None = None


def backtest_for(profile: Profile) -> Backtest | None:
    """Computed once per profile, then cached in memory and on disk (data/.cache/backtest.pkl). It does not
    depend on the forecast arguments. Invalidated when the data or this file changes."""
    global _BACKTESTS
    key = _backtest_inputs_hash()
    if _BACKTESTS is None:
        try:
            with open(CACHE_PATH, "rb") as f:
                saved_key, saved = pickle.load(f)
            _BACKTESTS = saved if saved_key == key else {}
        except Exception:
            _BACKTESTS = {}
    if profile.id not in _BACKTESTS:
        _BACKTESTS[profile.id] = run_backtest(profile)
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
            with open(tmp, "wb") as f:
                pickle.dump((key, _BACKTESTS), f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, CACHE_PATH)
        except OSError:
            pass
    return _BACKTESTS[profile.id]


# ------------------------------------------------------------------ 6. formulas
def _forecast(profile: Profile, horizon_months: int, paths: int | None, cushion: float | None, seed: int,
              changes: list[ForecastChange] | None, with_backtest: bool, daily_days: int = DAILY_DAYS) -> LiquidForecast:
    A_all = load_assumptions()
    A = {k: v["value"] for k, v in A_all.items()}
    paths = int(paths or A["forecast_paths"])
    key = _seed(profile.id, horizon_months, paths, cushion, seed, [c.model_dump_json() for c in changes or []])   # the caller's arguments only
    cushion = essential_monthly(profile) if cushion is None else cushion
    cal = _calibration() if OPTIONS["calibrate"] else None
    out = _summarize(profile, _simulate(profile, horizon_months, paths, cushion, key, A, changes, cal["month_end_factors"] if cal else None),
                     horizon_months, A_all, daily_days)
    if with_backtest:
        out = out.model_copy(update={"backtest": backtest_for(profile)})
    return out


class ForecastArgs(Base):
    horizon_months: int = Field(DEFAULT_HORIZON_MONTHS, ge=1, le=60, description="Months to forecast")
    paths: int | None = Field(None, ge=100, le=10000, description="Monte Carlo paths; default from assumptions.json")
    cushion: float | None = Field(None, ge=0, description="Dollar line for cushion_risk; default one month of essential expenses")
    seed: int = Field(0, description="Changes the random draws; the same arguments always give identical output")
    changes: list[ForecastChange] = Field(default_factory=list, description="What-ifs. Only lump_sum and income_change are implemented")
    with_backtest: bool = Field(True, description="Attach the cached backtest")


@register("forecast_liquid", args=ForecastArgs, result=LiquidForecast, category="forecast")
def forecast_liquid(profile: Profile, *, horizon_months: int = DEFAULT_HORIZON_MONTHS, paths: int | None = None,
                    cushion: float | None = None, seed: int = 0, changes: list[ForecastChange] | None = None,
                    with_backtest: bool = True) -> LiquidForecast:
    """Full forecast, for the API and charts.

    Two risk outputs: `overdraft_risk` (balance below $0, the urgent alert) and `cushion_risk` (below `cushion`, default one
    month of essential expenses; the default health indicator)."""
    return _forecast(profile, horizon_months, paths, cushion, seed, changes, with_backtest)


def forecast_for_chart(profile: Profile, horizon_months: int) -> LiquidForecast:
    """The same forecast as forecast_liquid (identical numbers, default arguments), but with a daily band for the whole horizon instead of
    the first DAILY_DAYS days, so a chart can plot one resolution throughout. Not registered: it is not a tool for the agents."""
    return _forecast(profile, horizon_months, None, None, 0, None, False, daily_days=horizon_months * 31)


@register("forecast_liquid_summary", args=ForecastArgs, result=LiquidForecast, category="forecast")
def forecast_liquid_summary(profile: Profile, **kwargs) -> LiquidForecast:
    """Same numbers without the daily and monthly arrays, so it is small enough for an agent's context."""
    return forecast_liquid(profile, **kwargs).model_copy(update={"daily": None, "monthly": None})

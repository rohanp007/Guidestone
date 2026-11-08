"""Read-only endpoints for the signed-in customer's own data. The customer comes from the session; there is no id in any URL."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import config
from ..auth import User, current_user
from ..data.loader import load_profiles
from ..data.models import Base, LiquidForecast, Profile
from ..formulas.forecast import forecast_liquid
from ..llm.facts import forecast_facts
from . import profiles
from .schemas import TransactionPage
from .serialize import SafeJSONResponse, to_plain

router = APIRouter(prefix="/api/me", tags=["me"])

FORECAST_MONTHS = 6          # 180 days
STATUS_LABELS = {"on_track": "On track", "watch": "Worth watching", "at_risk": "At risk"}
STATUS_NOTES = {"on_track": "Cash stays above a one-month cushion and is growing.",
                "watch": "Cash dips below a one-month cushion at times, or is not growing.",
                "at_risk": "Cash is below a one-month cushion, falling, or may overdraw."}


def own_profile(user: User = Depends(current_user)) -> Profile:
    profile = load_profiles().get(user.customer_id)
    if profile is None:
        raise HTTPException(status_code=403, detail="forbidden")
    return profile


_FORECASTS: dict[str, LiquidForecast] = {}      # deterministic (fixed seed), so one per customer is enough


def _forecast(profile: Profile) -> LiquidForecast:
    if profile.id not in _FORECASTS:
        _FORECASTS[profile.id] = forecast_liquid(profile, horizon_months=FORECAST_MONTHS, with_backtest=False)
    return _FORECASTS[profile.id]


@router.get("/profile", response_model=Profile)
def my_profile(profile: Profile = Depends(own_profile)) -> Profile:
    return profile


@router.get("/transactions", response_model=TransactionPage)
def my_transactions(
    profile: Profile = Depends(own_profile),
    category: str | None = None,
    flow_class: str | None = None,
    start: date | None = None,
    end: date | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1),
) -> TransactionPage:
    return profiles.get_transactions(profile=profile, account_id=None, category=category, flow_class=flow_class, start=start,
                                     end=end, q=q, page=page, page_size=page_size)


@router.get("/forecast", response_model=LiquidForecast)
def my_forecast(profile: Profile = Depends(own_profile)):
    """180-day liquid-cash forecast: daily bands for the first 90 days, month-end points after that."""
    return SafeJSONResponse(to_plain(_forecast(profile)))


class Status(Base):
    status: Literal["on_track", "watch", "at_risk"]
    label: str                             # plain language, for the summary row
    note: str


@router.get("/status", response_model=Status)
def my_status(profile: Profile = Depends(own_profile)) -> Status:
    """The cash-outlook status, decided in code from the forecast (the same rule the advisor uses). Fast: no model involved."""
    code = forecast_facts(_forecast(profile))["overall_status"]
    return Status(status=code, label=STATUS_LABELS[code], note=STATUS_NOTES[code])


class RecurringItem(Base):
    name: str
    group: Literal["income", "bill", "debt"]
    amount: float                          # positive dollars per occurrence
    monthly_amount: float                  # positive dollars per month
    cadence: str
    next_date: date


def _next_date(r, as_of: date) -> date:
    from calendar import monthrange
    if r.cadence == "biweekly":
        d = r.last_date
        while d <= as_of:
            d += timedelta(days=14)
        return d
    def on_day(y: int, m: int, day: int) -> date:
        return date(y, m, min(day, monthrange(y, m)[1]))
    # semimonthly pay lands twice a month, about 15 days apart: the last pay day and the one half a month away
    days = [r.typical_day] if r.cadence != "semimonthly" else sorted({r.last_date.day, (r.last_date.day + 14) % 30 + 1})
    y, m = as_of.year, as_of.month
    for _ in range(3):
        for d in days:
            cand = on_day(y, m, d)
            if cand > as_of:
                return cand
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return on_day(y, m, days[0])


@router.get("/recurring", response_model=list[RecurringItem])
def my_recurring(profile: Profile = Depends(own_profile)) -> list[RecurringItem]:
    """Income, bills and debt payments with the next expected date. Transfers to savings or investments are not bills."""
    group = {"income": "income", "housing": "bill", "utility": "bill", "insurance": "bill", "subscription": "bill",
             "loan_payment": "debt", "card_payment": "debt"}
    out = [RecurringItem(name=r.description, group=group[r.kind], amount=abs(r.amount_per_occurrence), monthly_amount=abs(r.monthly_amount),
                         cadence=r.cadence, next_date=_next_date(r, profile.as_of_date))
           for r in profile.recurring if r.kind in group]
    return sorted(out, key=lambda i: (["income", "bill", "debt"].index(i.group), i.next_date))

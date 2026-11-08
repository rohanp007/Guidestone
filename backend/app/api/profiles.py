from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from .. import config
from ..data.loader import load_profiles, transactions_for
from ..data.models import Profile
from ..formulas.health import paycheck_to_paycheck
from ..auth import User, current_user
from .deps import get_profile
from .schemas import ProfileListItem, Transaction, TransactionPage
from .serialize import to_plain

router = APIRouter(prefix="/api/profiles", tags=["profiles"])
MAX_PAGE_SIZE = 200


@lru_cache(maxsize=1)
def paycheck_flags() -> frozenset[str]:
    """Ids flagged paycheck-to-paycheck. Deterministic, so computed once."""
    return frozenset(cid for cid, p in load_profiles().items() if paycheck_to_paycheck(p).flag)


def _flags(p: Profile) -> list[str]:
    flags = []
    if p.id in config.FEATURED_PERSONAS: flags.append("featured")
    if p.id in paycheck_flags(): flags.append("paycheck_to_paycheck")
    if p.spending and p.spending.carries_card_balance: flags.append("revolver")
    if p.accounts_of("mortgage"): flags.append("has_mortgage")
    return flags


@router.get("", response_model=list[ProfileListItem])
def list_profiles(user: User = Depends(current_user)) -> list[ProfileListItem]:
    """Every customer's summary, for the demo's persona list. Featured personas come first, then everyone by id.
    Demo mode only: in production no one can list other customers."""
    if not config.auth_settings().demo_mode:
        raise HTTPException(status_code=403, detail="forbidden")
    featured = {cid: i for i, cid in enumerate(config.FEATURED_PERSONAS)}
    items = [ProfileListItem(id=p.id, age=p.age, employment=p.employment, housing=p.housing, credit_tier=p.credit_tier,
                             net_worth=p.summary.net_worth, monthly_surplus=p.summary.monthly_surplus, flags=_flags(p))
             for p in load_profiles().values()]
    return sorted(items, key=lambda i: (featured.get(i.id, len(featured)), i.id))


@router.get("/{id}", response_model=Profile)
def get_profile_detail(profile: Profile = Depends(get_profile)) -> Profile:
    """The full profile: summary, spending profile, recurring flows, history, holdings and estimated assets."""
    return profile


@router.get("/{id}/transactions", response_model=TransactionPage)
def get_transactions(
    profile: Profile = Depends(get_profile),
    account_id: str | None = None,
    category: str | None = None,
    flow_class: str | None = None,
    start: date | None = Query(None, description="First day, inclusive (ISO date)"),
    end: date | None = Query(None, description="Last day, inclusive (ISO date)"),
    q: str | None = Query(None, description="Case-insensitive search in the description"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, description=f"Capped at {MAX_PAGE_SIZE}"),
) -> TransactionPage:
    page_size = min(page_size, MAX_PAGE_SIZE)
    df = transactions_for(profile.id).copy()               # the cached frame is shared and read-only
    keep = pd.Series(True, index=df.index)
    if account_id: keep &= df["account_id"] == account_id
    if category: keep &= df["category"] == category
    if flow_class: keep &= df["flow_class"] == flow_class
    if start: keep &= df["posted_date"] >= pd.Timestamp(start)
    if end: keep &= df["posted_date"] < pd.Timestamp(end + timedelta(days=1))
    if q: keep &= df["description"].str.contains(q, case=False, regex=False)
    hits = df[keep]                                          # newest first, as transactions_for returns it
    rows = hits.iloc[(page - 1) * page_size: page * page_size]
    rows = rows.assign(posted_date=rows["posted_date"].dt.strftime("%Y-%m-%d"))       # a date, not a timestamp
    return TransactionPage(items=[Transaction(**r) for r in to_plain(rows[list(Transaction.model_fields)])], total=len(hits), page=page, page_size=page_size)

"""Load the Parquet files in data/ into validated Profile objects.

Public API:
    load_profiles() -> dict[str, Profile]      cached; fails loudly if data is missing or invalid
    transactions_for(customer_id) -> DataFrame newest first. READ-ONLY: it is the cached frame, not a copy;
                                      call .copy() before modifying or you will corrupt the cache.
"""
from __future__ import annotations

import hashlib
import math
import os
import pickle
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from . import derive
from ..config import AS_OF_DATE, DATA_DIR
from .models import (INSTALLMENT_TYPES, LIABILITY_TYPES, LIQUID_TYPES, Account, Dependent, Holding, Profile,
                     RetirementInfo, Summary)

_CREATED_BY = {name: "scripts/generate_bank_data.py, then scripts/to_parquet.py" for name in
               ("customers", "profiles", "accounts", "account_terms", "ira_holdings", "transactions", "monthly_statements")}
_CREATED_BY["bio"] = "scripts/generate_bio.py"
_CREATED_BY["assets"] = "scripts/generate_assets.py"

MAX_ERRORS_SHOWN = 25
CACHE_PATH = DATA_DIR / ".cache" / "profiles.pkl"


# ------------------------------------------------------------------ helpers
def _clean(v):
    """NaN / NaT / pd.NA / '' -> None. Lists and arrays pass through."""
    if v is None or v is pd.NaT or v is pd.NA:
        return None
    if isinstance(v, str):
        return v or None
    if isinstance(v, (list, tuple, np.ndarray)):
        return v
    return None if pd.isna(v) else v


def _date(v) -> date | None:
    v = _clean(v)
    if v is None: return None
    if isinstance(v, datetime): return v.date()          # includes pd.Timestamp
    if isinstance(v, date): return v
    return date.fromisoformat(str(v))


def _int(v) -> int | None:
    v = _clean(v)
    return None if v is None else int(v)


def _float(v) -> float | None:
    v = _clean(v)
    return None if v is None else float(v)


def months_until(d: date | None) -> int | None:
    """Whole months from AS_OF_DATE to d, never negative."""
    return derive.months_until(d, AS_OF_DATE)


# ------------------------------------------------------------------ raw tables
def _read(name: str) -> pd.DataFrame:
    path = DATA_DIR / f"{name}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing. Create it with {_CREATED_BY[name]}")
    t = pq.read_table(path)
    t = t.cast(pa.schema([f.with_type(pa.float64()) if pa.types.is_decimal(f.type) else f for f in t.schema]))
    df = t.to_pandas()
    for col in ("customer_id", "account_id"):
        if col in df: df[col] = df[col].astype(str)
    if "posted_date" in df: df["posted_date"] = pd.to_datetime(df["posted_date"])
    return df


@lru_cache(maxsize=1)
def _tables() -> dict:
    """Read all 8 files and group them by customer once, so building a profile never rescans a full table."""
    raw = {n: _read(n) for n in _CREATED_BY}

    def by_customer(df): return {k: g for k, g in df.groupby("customer_id", sort=False)}
    def records(df): return {k: g.to_dict("records") for k, g in df.groupby("customer_id", sort=False)}

    tx = raw["transactions"].sort_values(["posted_date", "transaction_id"], ascending=False)
    return dict(
        customers=raw["customers"].set_index("customer_id").to_dict("index"),
        profiles=raw["profiles"].set_index("customer_id").to_dict("index"),
        bio=raw["bio"].set_index("customer_id").to_dict("index"),
        assets=raw["assets"].set_index("customer_id").to_dict("index"),
        terms=raw["account_terms"].set_index("account_id").to_dict("index"),
        accounts=records(raw["accounts"]),
        holdings=records(raw["ira_holdings"]),
        statements=by_customer(raw["monthly_statements"]),
        transactions=by_customer(tx),
    )


def transactions_for(customer_id: str) -> pd.DataFrame:
    try:
        return _tables()["transactions"][customer_id]
    except KeyError:
        raise KeyError(f"no transactions for customer {customer_id!r}") from None


# ------------------------------------------------------------------ builders
def _account(row: dict, terms: dict, card_limit_fallback: float | None) -> Account:
    typ = row["account_type"]
    t = terms.get(row["account_id"], {})
    rate_pct = row["apr_pct"] if typ in LIABILITY_TYPES else row["annual_yield_pct"]
    renew = _clean(t.get("auto_renew"))
    maturity = _date(t.get("maturity_date"))
    limit = _float(t.get("credit_limit"))
    if typ == "credit_card" and limit is None:
        limit = card_limit_fallback
    return Account(
        id=row["account_id"], type=typ, subtype=_clean(t.get("subtype")) or "",
        balance=float(row["balance"]), opening_balance=float(row["opening_balance"]),
        rate=float(rate_pct) / 100, min_payment=float(row["monthly_payment"]),
        credit_limit=limit, open_date=_date(t.get("open_date")), original_amount=_float(t.get("original_amount")),
        term_months=_int(t.get("term_months")), term_months_remaining=months_until(maturity), maturity_date=maturity,
        auto_renew=None if renew is None else str(renew).lower() == "yes", penalty_days=_int(t.get("early_withdrawal_penalty_days")),
    )


def _estimated_assets(cid: str, a: dict) -> list[Account]:
    """Home / vehicle as non-cash Accounts. Values are assumptions (see scripts/generate_assets.py)."""
    out = []
    for typ, key in (("home", "home_value"), ("vehicle", "vehicle_value")):
        value = _float(a.get(key))
        if value is not None:
            out.append(Account(id=f"A-{cid}-{typ.upper()}", type=typ, subtype="estimated", balance=value,
                               opening_balance=_float(a.get(f"{key}_at_start")) or 0.0))
    return out


def _summary(p: Profile) -> Summary:
    cards = p.accounts_of("credit_card")
    limits = sum(a.credit_limit or 0 for a in cards)
    debt_pmts = sum(a.min_payment for a in p.liabilities)
    assets = sum(a.balance for a in p.accounts if not a.is_liability)
    debt = sum(a.balance for a in p.liabilities)
    return Summary(
        liquid_cash=sum(a.balance for a in p.accounts if a.type in LIQUID_TYPES),
        total_assets=assets, total_debt=debt, net_worth=assets - debt,
        net_monthly_income=p.net_monthly, monthly_non_debt_expenses=p.monthly_non_debt_expenses,
        monthly_debt_payments=debt_pmts, monthly_surplus=p.net_monthly - p.monthly_non_debt_expenses - debt_pmts,
        credit_utilization=sum(a.balance for a in cards) / limits if limits else None,
    )


def _build(cid: str, t: dict) -> Profile:
    c, pr, bio = t["customers"][cid], t["profiles"][cid], t["bio"][cid]
    card_limit = _float(pr.get("credit_limit")) or None
    accounts = [_account(r, t["terms"], card_limit) for r in t["accounts"].get(cid, [])]
    accounts += _estimated_assets(cid, t["assets"].get(cid, {}))
    holdings = [Holding(ticker=h["fund_ticker"], name=h["fund_name"], weight=h["weight_pct"] / 100, market_value=h["market_value"])
                for h in t["holdings"].get(cid, [])]
    profile = Profile(
        id=cid, as_of_date=AS_OF_DATE, age=int(c["age"]), employment=c["employment_status"], housing=c["housing_status"],
        state=bio["state"], credit_score=int(c["credit_score"]), credit_tier=c["credit_tier"], pay_frequency=c["pay_frequency"],
        dependents=[Dependent(age=int(a)) for a in list(bio["dependents"])],
        retirement=RetirementInfo(has_401k=bool(bio["has_401k"]), employer_match_pct=float(bio["employer_match_pct"]),
                                  target_age=int(bio["target_age"]), ira_type=_clean(c.get("ira_type")) or ""),
        overdraft_plug_deposits=int(c["overdraft_plug_deposits"]), overdraft_plug_withdrawals=int(c["overdraft_plug_withdrawals"]),
        gross_monthly=float(c["annual_gross_income"]) / 12, net_monthly=float(pr["monthly_income"]),
        monthly_non_debt_expenses=float(pr["monthly_non_debt_expenses"]),
        accounts=accounts, holdings=holdings,
    )
    profile = derive.enrich(profile, t["transactions"][cid], t["statements"][cid])
    return profile.model_copy(update={"summary": _summary(profile)})


def _problems(p: Profile) -> list[str]:
    out = []
    n_chk = len(p.accounts_of("checking"))
    if n_chk != 1: out.append(f"{n_chk} checking accounts (need exactly 1)")
    for a in p.liabilities:
        if a.balance > 0 and not 0 < a.rate < 0.40: out.append(f"{a.id}: rate {a.rate:.4f} not in (0, 0.40)")
        if a.type == "credit_card" and not a.credit_limit: out.append(f"{a.id}: card has no credit_limit")
        if a.type in INSTALLMENT_TYPES and a.balance > 0 and a.min_payment <= 0: out.append(f"{a.id}: loan with balance has no min_payment")
    if p.accounts_of("ira"):
        w = sum(h.weight for h in p.holdings)
        if abs(w - 1) > 0.01: out.append(f"IRA holding weights sum to {w:.4f}")
    s = p.summary
    if s is None or not math.isclose(s.net_worth, s.total_assets - s.total_debt, abs_tol=0.01):
        out.append("summary.net_worth != total_assets - total_debt")
    if derive.IMPLEMENTED and not any(r.monthly_amount > 0 for r in p.recurring):
        out.append("no recurring inflow found")
    return out


def cache_key() -> str:
    """Hash of the timestamps and sizes of every input file AND the code that builds profiles, so regenerating
    data or editing derive / loader / models invalidates the cache automatically."""
    here = Path(__file__).resolve().parent
    files = [DATA_DIR / f"{n}.parquet" for n in sorted(_CREATED_BY)] + [here / "derive.py", here / "loader.py", here / "models.py", here.parent / "config.py"]
    h = hashlib.sha256()
    for f in files:
        st = f.stat()
        h.update(f"{f.name}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()


def _read_cache(key: str) -> dict[str, Profile] | None:
    try:
        with open(CACHE_PATH, "rb") as f:
            cached_key, profiles = pickle.load(f)
        return profiles if cached_key == key else None
    except Exception:          # missing, corrupt, or written by a different module layout: just rebuild
        return None


def _write_cache(key: str, profiles: dict[str, Profile]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
        with open(tmp, "wb") as f:
            pickle.dump((key, profiles), f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, CACHE_PATH)      # atomic: a reader never sees a half-written file
    except OSError:
        pass                              # read-only disk: the cache is an optimization only


@lru_cache(maxsize=1)
def load_profiles() -> dict[str, Profile]:
    """Cached on disk (data/.cache/profiles.pkl). Only a fully validated load is ever cached."""
    key = cache_key()
    profiles = _read_cache(key)
    if profiles is None:
        profiles = _build_all()
        _write_cache(key, profiles)
    return profiles


def _build_all() -> dict[str, Profile]:
    t = _tables()
    profiles: dict[str, Profile] = {}
    errors: list[str] = []
    for cid in sorted(t["customers"]):
        missing = [n for n in ("profiles", "bio", "assets") if cid not in t[n]]
        if missing:
            errors.append(f"{cid}: no row in {', '.join(missing)} ({_CREATED_BY[missing[0]]})")
            continue
        try:
            p = _build(cid, t)
        except (ValidationError, KeyError, ValueError) as e:
            errors.append(f"{cid}: could not build profile: {e}")
            continue
        errors += [f"{cid}: {m}" for m in _problems(p)]
        profiles[cid] = p
    if errors:
        shown = "\n  ".join(errors[:MAX_ERRORS_SHOWN])
        more = f"\n  ... and {len(errors) - MAX_ERRORS_SHOWN} more" if len(errors) > MAX_ERRORS_SHOWN else ""
        raise ValueError(f"{len(errors)} data problem(s) while loading profiles:\n  {shown}{more}")
    return profiles

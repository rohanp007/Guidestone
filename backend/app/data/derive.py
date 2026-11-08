"""Fills the Profile fields that come from transaction history: recurring, variable_spend(_checking), history.

Does not touch `summary`; the loader builds that from the finished Profile.

Money flow model, so nothing is counted twice:
  * recurring              fixed-ish flows in CHECKING (pay, rent, bills, loan + card payments, savings/IRA transfers)
  * variable_spend         every other expense, checking + card purchases: what the customer spends, by category
  * variable_spend_checking  the checking-only part of that: what actually leaves checking besides recurring.
    Card purchases reach checking through the recurring card payment, so cash-flow forecasts must use
    recurring + variable_spend_checking, never recurring + variable_spend.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .models import (LIABILITY_TYPES, LIQUID_TYPES, CategoryShare, MonthPoint, Profile, Recurring, SpendingProfile,
                     Subscription, SweepRule, TrendNote)

IMPLEMENTED = True   # the loader also checks that every profile has a recurring inflow

MIN_MONTHS_SEEN = 10   # a flow must show up in at least this many of the 24 months to count as recurring
SEASON_SHRINK = 0.5       # keep half of the observed seasonal swing: each calendar month is seen only twice
TREND_MIN_T = 2.0         # report a trend only if |slope| >= this many standard errors
NOTABLE_TREND_PCT = 0.10      # ...and agents may only MENTION one if it is at least 10%/yr
NOTABLE_TREND_DOLLARS = 25.0  # ...and at least $25/mo of change, so "entertainment is rising" never means $3
SWEEP_MIN_EVENTS = 2         # a sweep habit needs at least this many sweeps in the history
SEASON_MIN_MONTHLY = 20.0     # categories averaging under this many $/mo skip seasonality (factor 1.0): too small to matter, saves load time
CARRY_MONTHS = 3          # card "carries a balance" if it posted interest in at least this many months
TOP_CATEGORIES = 5
FIXED_AMOUNT_CV = 0.02    # a bill whose monthly amount varies less than this is "fixed" (rent, loan, subscription) and gets no residuals
NOISY_KINDS = frozenset({"utility", "insurance", "housing", "subscription"})
# Share of the observed calendar-month swing kept for a utility bill. 0.0 = OFF (all factors 1.0). Measured on the backtest
# (180-day median error / raw month-end coverage): 0.0 -> 6.2% / 0.60, 0.5 -> 6.8% / 0.58, 1.0 -> 7.1% / 0.55. It made the forecast
# worse, so it ships off. Re-test after the July shift (an unexplained ~4% outflow over-forecast from July) is understood.
UTILITY_SEASON_SHRINK = 0.0
ACTIVE_WITHIN_DAYS = 35  # ...and its last occurrence must be this recent, else it has ended (loan paid off, sub cancelled)

# Only these categories can be recurring. Merchant spending (groceries, dining, ...) hits many months too,
# but its amounts are discretionary, so it belongs in variable_spend, not here.
RECURRING_CATEGORIES = frozenset({
    "payroll", "retirement_income", "housing", "utilities", "insurance", "subscriptions",
    "debt_payment", "savings_contribution", "investment_contribution",
})
# Categories whose description is not stable month to month (the insurer varies), so group by category instead.
GROUP_BY_CATEGORY = frozenset({"payroll", "retirement_income", "insurance"})

_KIND = {
    "payroll": "income", "retirement_income": "income", "housing": "housing", "utilities": "utility",
    "insurance": "insurance", "subscriptions": "subscription", "savings_contribution": "savings_transfer",
    "investment_contribution": "investment_transfer",
}


def _kind(category: str, description: str) -> str:
    if category == "debt_payment":
        return "card_payment" if "Credit Card" in description else "loan_payment"
    return _KIND[category]


def _cadence(per_month: float) -> str:
    return ("irregular" if per_month < 0.8 else "monthly" if per_month < 1.3 else "semimonthly" if 1.9 <= per_month <= 2.1
            else "biweekly" if 2.1 < per_month <= 2.35 else "irregular")


def _tag(tx: pd.DataFrame) -> pd.DataFrame:
    """Add grouping columns; `candidate` marks rows that may belong to a recurring flow."""
    tx = tx.copy()
    tx["sign"] = np.where(tx["balance_change"] > 0, 1, -1)
    tx["desc"] = tx["description"].str.removesuffix(" (debit)").str.strip()
    tx["key"] = tx["desc"].where(~tx["category"].isin(GROUP_BY_CATEGORY), tx["category"])
    tx["month"] = tx["posted_date"].dt.to_period("M")
    tx["candidate"] = ((tx["account_type"] == "checking") & (tx["flow_class"] != "non_cash")
                       & tx["category"].isin(RECURRING_CATEGORIES))
    return tx


def _month_factors(ratios: pd.Series, calendar_month) -> list[float]:
    """Jan..Dec multipliers for one bill, from its ratio to its own mean in each history month. Each calendar month is seen only
    once or twice in 24 months, so each factor is shrunk halfway to 1.0 (as spending seasonality is) and the 12 are rescaled to
    average 1.0, so the annual total is unchanged. A calendar month never seen gets 1.0."""
    by = pd.Series(np.asarray(ratios), index=np.asarray(calendar_month)).groupby(level=0).mean()
    raw = np.array([by.get(m, 1.0) for m in range(1, 13)])
    shrunk = 1 + UTILITY_SEASON_SHRINK * (raw - 1)
    return (shrunk / shrunk.mean()).tolist()


def _recurring(tx: pd.DataFrame, as_of: date | None = None, months: pd.PeriodIndex | None = None,
               ended_out: list[Recurring] | None = None) -> tuple[list[Recurring], pd.Series]:
    """Returns the active recurring flows and a boolean mask of the rows that belong to them.

    Flows that ended before `as_of` are left out: `recurring` is the base for forecasting, so a paid-off
    loan must not keep reserving cash. Their rows fall through to variable_spend (expenses) or are ignored. When `ended_out`
    is given, the ended flows are appended to it (for display only)."""
    found, kept = [], []
    for (key, sign), g in tx[tx["candidate"]].groupby(["key", "sign"], sort=False):
        months_seen = g["month"].nunique()
        if months_seen < MIN_MONTHS_SEEN:
            continue
        last_date = g["posted_date"].max().date()
        ended = bool(as_of and (as_of - last_date).days > ACTIVE_WITHIN_DAYS)
        if ended and ended_out is None:
            continue
        if not ended:
            kept.append((key, sign))
        first = g["posted_date"].min()
        span_months = (last_date.year - first.year) * 12 + last_date.month - first.month + 1
        per_occurrence = float(g["balance_change"].median())      # the typical payment, for display
        per_month = len(g) / span_months                           # not months_seen: a payment that slips past month-end would inflate it
        desc, category = g["desc"].value_counts().index[0], g["category"].value_counts().index[0]
        kind = _kind(category, desc)
        residuals: list[float] = []
        month_factors: list[float] = []
        if months is not None and kind in NOISY_KINDS:
            monthly = g.groupby("month")["balance_change"].sum().reindex(months).fillna(0.0)
            paid = monthly[monthly != 0]
            if len(paid) >= 2 and paid.mean() != 0 and (paid / paid.mean()).std() >= FIXED_AMOUNT_CV:
                resid = ((monthly / paid.mean()) - 1).where(monthly != 0, 0.0)
                if kind == "utility":                              # heating / cooling: calendar-month factors, noise is what is left
                    month_factors = _month_factors(1 + resid[monthly != 0], monthly.index[monthly != 0].month)
                    cal = np.asarray(monthly.index.month) - 1
                    resid = ((1 + resid) / np.asarray(month_factors)[cal] - 1).where(monthly != 0, 0.0)
                residuals = resid.tolist()
        (ended_out if ended else found).append(Recurring(
            description=desc, category=category, kind=kind, cadence=_cadence(per_month), residuals=residuals, month_factors=month_factors,
            monthly_amount=float(g["balance_change"].sum()) / span_months, amount_per_occurrence=per_occurrence,
            occurrences_per_month=per_month, typical_day=int(round(g["posted_date"].dt.day.median())),
            months_seen=months_seen, last_date=last_date, account_id=g["account_id"].iat[0],
        ))
    found.sort(key=lambda r: (r.monthly_amount < 0, -abs(r.monthly_amount)))
    if ended_out is not None:
        ended_out.sort(key=lambda r: r.last_date, reverse=True)
    in_recurring = pd.Series(list(zip(tx["key"], tx["sign"])), index=tx.index).isin(kept) & tx["candidate"]
    return found, in_recurring


def detect_recurring(transactions: pd.DataFrame, as_of: date | None = None) -> list[Recurring]:
    """Fixed-amount-ish cash flows in CHECKING seen in >= MIN_MONTHS_SEEN months and still active at `as_of`.

    monthly_amount is the observed average per month (total / months from first to last occurrence), so it is an
    unbiased forecast even for flows whose amount swings (card payoffs, seasonal utilities); amount_per_occurrence
    is the median, the typical single payment.
    Inflows first, then outflows, each largest first.
    """
    return _recurring(_tag(transactions), as_of)[0] if not transactions.empty else []


def _variable(tx: pd.DataFrame, in_recurring: pd.Series, n_months: int, checking_only: bool) -> dict[str, float]:
    spend = tx[(tx["flow_class"] == "expense") & ~in_recurring]
    if checking_only:
        spend = spend[spend["account_type"] == "checking"]
    return (spend.groupby("category")["amount"].sum() / n_months).to_dict()


def _history(profile: Profile, tx: pd.DataFrame, statements: pd.DataFrame) -> list[MonthPoint]:
    """One point per statement month, oldest first. Home / vehicle values are interpolated between
    their opening (history start) and current estimates, since they have no statements."""
    st = statements.assign(
        bucket=statements["account_type"].map(lambda t: "debt" if t in LIABILITY_TYPES else "liquid" if t in LIQUID_TYPES else "asset"))
    bal = st.groupby(["month", "bucket"])["closing_balance"].sum().unstack(fill_value=0.0)
    for b in ("liquid", "asset", "debt"):
        if b not in bal: bal[b] = 0.0

    flows = tx.assign(m=tx["posted_date"].dt.strftime("%Y-%m"))
    income = flows[flows["flow_class"] == "income"].groupby("m")["amount"].sum()
    expenses = flows[flows["flow_class"] == "expense"].groupby("m")["amount"].sum()

    estimated = [a for a in profile.accounts if a.is_estimated]
    start, span = min(bal.index), (profile.as_of_date - min(bal.index)).days

    out = []
    for month, row in bal.sort_index().iterrows():
        month_end = (pd.Timestamp(month) + pd.offsets.MonthEnd(0)).date()
        frac = min(1.0, max(0.0, (month_end - start).days / span)) if span else 1.0
        est = sum(a.opening_balance * (a.balance / a.opening_balance) ** frac if a.opening_balance > 0 else a.balance
                  for a in estimated)
        key = month.strftime("%Y-%m")
        assets = float(row["liquid"] + row["asset"]) + est
        out.append(MonthPoint(month=key, liquid=float(row["liquid"]), total_assets=assets, total_debt=float(row["debt"]),
                              net_worth=assets - float(row["debt"]), income=float(income.get(key, 0.0)),
                              expenses=float(expenses.get(key, 0.0))))
    return out


def months_until(d: date | None, as_of: date) -> int | None:
    """Whole months from as_of to d, never negative."""
    if d is None:
        return None
    return max(0, (d.year - as_of.year) * 12 + d.month - as_of.month - (d.day < as_of.day))


def truncate(profile: Profile, transactions: pd.DataFrame, statements: pd.DataFrame, cutoff: date):
    """The profile and tables as they stood at `cutoff` (a month end), so the backtest can re-run enrich() on
    only the history that existed then. Ledger balances come from the statements; home / vehicle estimates are
    left as they are (they never touch liquid cash)."""
    tx = transactions[transactions["posted_date"] <= pd.Timestamp(cutoff)]
    periods = pd.PeriodIndex(pd.to_datetime(statements["month"]), freq="M")
    st = statements[periods <= pd.Period(cutoff, "M")]
    closing = st[st["month"] == st["month"].max()].set_index("account_id")["closing_balance"]
    accounts = [a.model_copy(update={"balance": float(closing.get(a.id, a.balance)),
                                     "term_months_remaining": months_until(a.maturity_date, cutoff)}) for a in profile.accounts]
    return profile.model_copy(update={"as_of_date": cutoff, "accounts": accounts}), tx, st


# ------------------------------------------------------------------ spending profile
def _monthly(rows: pd.DataFrame, months: pd.PeriodIndex) -> pd.DataFrame:
    """Monthly spend per category (rows = months, columns = categories); months without spend are 0."""
    return rows.groupby(["month", "category"])["amount"].sum().unstack(fill_value=0.0).reindex(months, fill_value=0.0)


def _seasonality(by_cat: pd.DataFrame) -> tuple[dict[str, dict[int, float]], dict[str, dict[int, float]]]:
    """Per category and calendar month: that month's average / the category's average, shrunk halfway to 1.0, and the
    standard error of that factor (the category's own month-to-month spread / sqrt(observations of that month), shrunk the
    same way). One groupby for all categories (this ran per category and was the slowest part of loading)."""
    mean = by_cat.mean()
    raw = by_cat.groupby(by_cat.index.month).mean().div(mean).reindex(range(1, 13))
    shrunk = (1 + SEASON_SHRINK * (raw - 1)).fillna(1.0)
    counts = by_cat.groupby(by_cat.index.month).size().reindex(range(1, 13)).astype(float)
    se = pd.DataFrame(np.outer(1 / np.sqrt(counts.fillna(np.inf)), by_cat.std() / mean) * SEASON_SHRINK, index=range(1, 13), columns=by_cat.columns)
    flat = mean < SEASON_MIN_MONTHLY
    shrunk.loc[:, flat] = 1.0
    se.loc[:, flat] = 0.0
    as_dict = lambda df: {c: {int(m): float(v) for m, v in df[c].items()} for c in df}
    return as_dict(shrunk), as_dict(se)


def _trend(x: pd.Series, factors: dict[int, float]) -> tuple[float, float]:
    """(annualized change as a decimal, $/mo change over a year) of the deseasonalized series;
    both 0 unless the slope clears TREND_MIN_T standard errors."""
    y = x.to_numpy() / np.array([factors[p.month] for p in x.index])
    n, t = len(y), np.arange(len(y))
    tc = t - t.mean()
    slope = (tc * y).sum() / (tc ** 2).sum()
    se = np.sqrt(((y - y.mean() - slope * tc) ** 2).sum() / (n - 2) / (tc ** 2).sum())
    if y.mean() <= 0 or abs(slope) < TREND_MIN_T * se:
        return 0.0, 0.0
    return float(slope * 12 / y.mean()), float(slope * 12)


def aggregate_seasonality(weights: dict[str, float], seasonality: dict[str, dict[int, float]]) -> dict[int, float]:
    """Spend-weighted seasonal index per calendar month, e.g. weights=variable_spend_checking. Multiply a
    deseasonalized monthly total (monthly_variable_samples) by it to get that month's actual total."""
    total = sum(weights.values())
    if total <= 0:
        return {m: 1.0 for m in range(1, 13)}
    return {m: sum(w * seasonality.get(c, {}).get(m, 1.0) for c, w in weights.items()) / total for m in range(1, 13)}


def aggregate_seasonality_se(weights: dict[str, float], se: dict[str, dict[int, float]]) -> dict[int, float]:
    """Standard error of the spend-weighted seasonal index (categories are independent)."""
    total = sum(weights.values())
    if total <= 0:
        return {m: 0.0 for m in range(1, 13)}
    return {m: float(np.sqrt(sum((w / total) ** 2 * se.get(c, {}).get(m, 0.0) ** 2 for c, w in weights.items()))) for m in range(1, 13)}


def _sweep_rule(tx: pd.DataFrame) -> SweepRule | None:
    """Learned from the customer's own outflows to an outside account (the checking-only saver sweeps to savings, which
    never leaves liquid cash and so isn't a rule worth modeling)."""
    sweeps = tx[(tx["account_type"] == "checking") & (tx["category"] == "external_cash_management") & (tx["balance_change"] < 0)]
    if len(sweeps) < SWEEP_MIN_EVENTS:
        return None
    before = sweeps["balance_after"] - sweeps["balance_change"]
    threshold, target = float(before.min()), float(sweeps["balance_after"].median())
    if threshold <= target:
        return None
    return SweepRule(threshold=threshold, target=target, day=int(round(sweeps["posted_date"].dt.day.median())), events=len(sweeps))


def _subscriptions(tx: pd.DataFrame, as_of: date) -> list[Subscription]:
    subs = []
    for name, g in tx[(tx["category"] == "subscriptions") & (tx["flow_class"] == "expense")].groupby("desc"):
        if g["month"].nunique() >= MIN_MONTHS_SEEN and (as_of - g["posted_date"].max().date()).days <= ACTIVE_WITHIN_DAYS:
            subs.append(Subscription(name=name, monthly_amount=float(g["amount"].median())))
    return sorted(subs, key=lambda s: -s.monthly_amount)


def _spending(profile: Profile, tx: pd.DataFrame, in_recurring: pd.Series, statements: pd.DataFrame,
              months: pd.PeriodIndex) -> SpendingProfile:
    n = len(months)
    expense = tx[tx["flow_class"] == "expense"]
    variable = expense[~in_recurring[expense.index]]
    by_cat = _monthly(variable, months)
    by_cat = by_cat.loc[:, by_cat.mean() > 0]

    seasonality, seasonality_se = _seasonality(by_cat)
    volatility = (by_cat.std() / by_cat.mean()).astype(float).to_dict()
    trends = {c: _trend(by_cat[c], seasonality[c]) for c in by_cat}
    trend = {c: pct for c, (pct, _) in trends.items()}
    notable = sorted((TrendNote(category=c, annual_change=pct, monthly_change=dollars) for c, (pct, dollars) in trends.items()
                      if abs(pct) >= NOTABLE_TREND_PCT and abs(dollars) >= NOTABLE_TREND_DOLLARS),
                     key=lambda t: -abs(t.monthly_change))

    chk = _monthly(variable[variable["account_type"] == "checking"], months)
    index = aggregate_seasonality(profile.variable_spend_checking, seasonality)
    samples = [float(chk.loc[p].sum() / index[p.month]) for p in months]
    total_index = aggregate_seasonality(profile.variable_spend, seasonality)
    total_samples = [float(by_cat.loc[p].sum() / total_index[p.month]) for p in months]

    card = tx[tx["account_type"] == "credit_card"]
    carry = int(card[card["category"] == "debt_interest"]["month"].nunique())
    var_total = variable["amount"].sum()
    card_share = float(variable[variable["account_type"] == "credit_card"]["amount"].sum() / var_total) if var_total else 0.0

    subs = _subscriptions(tx, profile.as_of_date)

    c = tx[tx["account_type"] == "checking"]
    outs = c[(c["balance_change"] < 0) & c["category"].isin(["savings_contribution", "external_cash_management"])]
    per_month = outs.groupby("month").size()
    planned = any(r.kind == "savings_transfer" for r in profile.recurring)   # a planned deposit accounts for one transfer a month
    sweeps = int((per_month - 1).clip(lower=0).sum() if planned else per_month.sum())
    years = n / 12

    st = statements[statements["account_type"] == "checking"].assign(month=lambda d: pd.PeriodIndex(pd.to_datetime(d["month"]), freq="M")).set_index("month")
    lowest = pd.concat([c.groupby("month")["balance_after"].min(), st["opening_balance"]], axis=1).min(axis=1)
    ratios = (lowest / st["balance_decreases"]).replace([np.inf, -np.inf], np.nan).dropna()

    totals = expense.groupby("category")["amount"].sum() / n
    top = [CategoryShare(category=cat, monthly=float(v), share=float(v / totals.sum())) for cat, v in totals.nlargest(TOP_CATEGORIES).items()]

    return SpendingProfile(
        seasonality=seasonality, seasonality_se=seasonality_se, sweep_rule=_sweep_rule(tx), volatility=volatility, trend=trend, notable_trends=notable, monthly_variable_samples=samples, monthly_total_samples=total_samples,
        card_share=card_share, carries_card_balance=carry >= CARRY_MONTHS, card_carry_months=carry,
        subscriptions=subs, subscription_total=sum(s.monthly_amount for s in subs),
        savings_sweeps_per_year=sweeps / years, savings_pulls_per_year=int((c["category"] == "savings_withdrawal").sum()) / years,
        overdraft_topups=int(((c["category"] == "external_cash_management") & (c["balance_change"] > 0)).sum()),
        buffer_ratio_min=float(ratios.min()) if len(ratios) else 0.0, buffer_ratio_median=float(ratios.median()) if len(ratios) else 0.0,
        top_categories=top,
    )


def enrich(profile: Profile, transactions: pd.DataFrame, statements: pd.DataFrame) -> Profile:
    if transactions.empty:
        return profile
    tx = _tag(transactions)
    months = pd.PeriodIndex(pd.to_datetime(statements["month"]), freq="M").unique().sort_values()
    ended: list[Recurring] = []
    recurring, in_recurring = _recurring(tx, profile.as_of_date, months, ended)
    n_months = statements["month"].nunique() or tx["month"].nunique()
    profile = profile.model_copy(update={
        "recurring": recurring,
        "ended_recurring": ended,
        "variable_spend": _variable(tx, in_recurring, n_months, checking_only=False),
        "variable_spend_checking": _variable(tx, in_recurring, n_months, checking_only=True),
        "history": _history(profile, tx, statements),
    })
    return profile.model_copy(update={"spending": _spending(profile, tx, in_recurring, statements, months)})

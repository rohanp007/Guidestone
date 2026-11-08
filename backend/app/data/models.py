"""Profile contract. Every layer (loader, formulas, engine, agents, API, frontend types)
builds against these shapes. Change them here first, then everywhere else.

Conventions:
- Rates are DECIMALS (0.2399 = 23.99%). Liabilities: APR. Savings/CD: yield. IRA: expected return.
- Liability balances are POSITIVE amounts owed.
- Money in dollars (float). Round only for display.
- Transactions are NOT on Profile; they stay in a DataFrame and are paged by the API.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountType = Literal[
    "checking", "savings", "cd", "ira",
    "credit_card", "auto_loan", "personal_loan", "mortgage",
    "home", "vehicle",   # estimated non-cash assets: assumptions, not ledger data
]
LIABILITY_TYPES: frozenset[str] = frozenset({"credit_card", "auto_loan", "personal_loan", "mortgage"})
LIQUID_TYPES: frozenset[str] = frozenset({"checking", "savings"})
INSTALLMENT_TYPES: frozenset[str] = frozenset({"auto_loan", "personal_loan", "mortgage"})
ESTIMATED_TYPES: frozenset[str] = frozenset({"home", "vehicle"})   # count in total_assets / net worth, never in liquid_cash

CreditTier = Literal["poor", "fair", "good", "very_good", "exceptional"]
PayFrequency = Literal["monthly", "semimonthly", "biweekly"]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Account(Base):
    id: str
    type: AccountType
    subtype: str = ""                      # high_yield, revolving, fixed_30yr, roth, promotional, ...
    balance: float = Field(ge=0)           # liabilities: amount owed
    opening_balance: float = 0.0           # balance at history_start
    rate: float = Field(0.0, ge=0, le=1)   # decimal
    min_payment: float = Field(0.0, ge=0)  # monthly; 0 for asset accounts
    credit_limit: float | None = None      # cards only
    open_date: date | None = None
    original_amount: float | None = None   # loans / CDs
    term_months: int | None = None
    term_months_remaining: int | None = None
    maturity_date: date | None = None      # loans / CDs
    auto_renew: bool | None = None         # CDs
    penalty_days: int | None = None        # CD early-withdrawal penalty in days of interest

    @property
    def is_liability(self) -> bool:
        return self.type in LIABILITY_TYPES

    @property
    def is_liquid(self) -> bool:
        return self.type in LIQUID_TYPES

    @property
    def is_estimated(self) -> bool:
        """Home / vehicle value: label "Estimated value" in the UI."""
        return self.type in ESTIMATED_TYPES

    @property
    def utilization(self) -> float | None:
        if self.type == "credit_card" and self.credit_limit:
            return self.balance / self.credit_limit
        return None


RecurringKind = Literal[
    "income", "housing", "utility", "insurance", "subscription",
    "loan_payment", "card_payment", "savings_transfer", "investment_transfer",
]
Cadence = Literal["monthly", "semimonthly", "biweekly", "irregular"]


class Recurring(Base):
    """A cash flow that repeats (derived from checking transactions)."""
    description: str
    category: str
    kind: RecurringKind                    # what it is, for agents / UI; derived from category (+ description for debt)
    cadence: Cadence                       # from occurrences_per_month: <1.3 monthly, ~2.0 semimonthly, ~2.2 biweekly
    monthly_amount: float                  # signed: + inflow, - outflow; observed average per month (not median x count)
    amount_per_occurrence: float           # signed; median, the typical single payment
    occurrences_per_month: float = 1.0     # biweekly pay ~2.17
    typical_day: int = Field(ge=1, le=31)
    months_seen: int = Field(ge=1, le=24)
    last_date: date                        # most recent occurrence in the history
    account_id: str
    residuals: list[float] = Field(default_factory=list)   # month-by-month deviation from this flow's own mean (0.08 = 8% above), aligned with
                                                           # the history months; only for bills whose amount actually varies (utilities, ...)


class MonthPoint(Base):
    """One month of history, from monthly_statements."""
    month: str                             # "YYYY-MM"
    liquid: float
    total_assets: float
    total_debt: float
    net_worth: float
    income: float                          # flow_class == income
    expenses: float                        # flow_class == expense


class Holding(Base):
    ticker: str
    name: str
    weight: float = Field(ge=0, le=1)      # decimal
    market_value: float


class Dependent(Base):
    age: int = Field(ge=0, le=30)


class RetirementInfo(Base):
    has_401k: bool = False
    employer_match_pct: float = Field(0.0, ge=0, le=1)  # decimal of salary matched
    target_age: int = Field(67, ge=50, le=90)
    ira_type: Literal["roth", "traditional", ""] = ""


class Subscription(Base):
    name: str
    monthly_amount: float = Field(ge=0)


class CategoryShare(Base):
    category: str
    monthly: float                         # average monthly $
    share: float = Field(ge=0, le=1)       # of total non-debt spending


class TrendNote(Base):
    """A spending trend big enough to mention: >= 10%/yr AND >= $25/mo of change (see derive.NOTABLE_TREND_*)."""
    category: str
    annual_change: float                   # decimal, signed (0.15 = +15%/yr)
    monthly_change: float                  # signed $/mo added to monthly spend over a year


class SweepRule(Base):
    """A customer's habit of moving surplus cash out of checking (to an outside account), learned from their own sweeps:
    on `day` of the month, if checking is above `threshold`, move the excess over `target`."""
    threshold: float                       # lowest balance a sweep was ever triggered at
    target: float                          # median balance left after a sweep
    day: int                               # typical day of month
    events: int                            # sweeps seen in the history


class SpendingProfile(Base):
    """How the customer spends, learned from 24 months of history. Feeds forecasts and the Monte Carlo.
    Variable categories only (recurring bills are in Profile.recurring). Fractions are decimals."""
    seasonality: dict[str, dict[int, float]]   # category -> calendar month 1-12 -> multiplier (shrunk halfway to 1.0: 2 observations per month)
    volatility: dict[str, float]               # category -> coefficient of variation of monthly spend (std / mean)
    trend: dict[str, float]                    # category -> annualized change as a decimal (0.05 = +5%/yr); 0 if |slope| < 1 std error
    seasonality_se: dict[str, dict[int, float]] = Field(default_factory=dict)   # standard error of each seasonality factor: wider for calendar
                                               # months seen once than twice, so forecasts can draw plausible factors instead of trusting them
    sweep_rule: SweepRule | None = None
    notable_trends: list[TrendNote] = Field(default_factory=list)   # the only trends agents should mention; largest $ first
    monthly_variable_samples: list[float]      # 24 months, oldest first, of variable spend paid from checking, deseasonalized
                                               # (multiply by derive.aggregate_seasonality(...)[month] to get the actual total back)
    monthly_total_samples: list[float] = Field(default_factory=list)   # same, but ALL variable spend (checking + card purchases), seasonalized
                                               # with weights = Profile.variable_spend; what the forecast bootstraps when the card is modeled
    card_share: float = Field(ge=0, le=1)      # card purchases / all variable spend
    carries_card_balance: bool                 # card posted interest in >= 3 months
    card_carry_months: int = Field(ge=0, le=24)
    subscriptions: list[Subscription] = Field(default_factory=list)   # checking + card, still active
    subscription_total: float = 0.0
    savings_sweeps_per_year: float = 0.0       # transfers out of checking beyond the planned monthly one (or to an outside account)
    savings_pulls_per_year: float = 0.0        # savings -> checking transfers to cover a shortfall
    overdraft_topups: int = 0                  # outside-account deposits into checking
    buffer_ratio_min: float                    # each month's lowest checking balance / that month's checking outflow
    buffer_ratio_median: float
    top_categories: list[CategoryShare] = Field(default_factory=list)  # top 5 by average spend


class Summary(Base):
    """Precomputed once at load; the dashboard and the agents' first prompt read this."""
    liquid_cash: float
    total_assets: float
    total_debt: float
    net_worth: float
    net_monthly_income: float
    monthly_non_debt_expenses: float
    monthly_debt_payments: float
    monthly_surplus: float                 # net income - non-debt expenses - debt payments
    credit_utilization: float | None       # None if no card


class Profile(Base):
    id: str
    as_of_date: date

    # biographical (customers.csv + bio side file)
    age: int
    employment: Literal["employed", "retired"]
    housing: Literal["renter", "owner_with_mortgage", "owner_outright"]
    state: str = "PA"
    credit_score: int = Field(ge=300, le=850)
    credit_tier: CreditTier
    pay_frequency: PayFrequency
    dependents: list[Dependent] = Field(default_factory=list)
    retirement: RetirementInfo = Field(default_factory=RetirementInfo)
    overdraft_plug_deposits: int = 0
    overdraft_plug_withdrawals: int = 0

    # income (gross for DTI/housing ratio, net for cash flow)
    gross_monthly: float = Field(gt=0)
    net_monthly: float = Field(gt=0)
    monthly_non_debt_expenses: float = Field(ge=0)  # profiles.csv: avg monthly flow_class=expense

    # money
    accounts: list[Account]
    recurring: list[Recurring] = Field(default_factory=list)
    variable_spend: dict[str, float] = Field(default_factory=dict)  # category -> avg monthly $ (non-recurring), checking + card
    variable_spend_checking: dict[str, float] = Field(default_factory=dict)  # same, but only what leaves checking (card spend reaches
                                                                              # checking via the recurring card payment)
    history: list[MonthPoint] = Field(default_factory=list)
    holdings: list[Holding] = Field(default_factory=list)
    spending: SpendingProfile | None = None   # filled by derive.py
    summary: Summary | None = None         # filled by derive.py

    # convenience lookups (no math)
    def account(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def accounts_of(self, *types: str) -> list[Account]:
        return [a for a in self.accounts if a.type in types]

    @property
    def liabilities(self) -> list[Account]:
        return [a for a in self.accounts if a.is_liability]

    @property
    def checking(self) -> Account:
        return next(a for a in self.accounts if a.type == "checking")


# ------------------------------------------------------------------ liquid-cash forecast (formulas/forecast.py)
class PercentileBands(Base):
    p10: list[float]
    p50: list[float]
    p90: list[float]


class MonthEndPoint(Base):
    month: str                             # "YYYY-MM"
    day: date                              # the month-end date
    p10: float
    p50: float
    p90: float


class LowPoint(Base):
    day: date                              # day with the lowest p50 balance
    p50: float
    p10: float


class Shortfall(Base):
    buffer: float                          # a path "falls short" when liquid cash drops below this ($0 for overdraft_risk)
    probability: float                     # share of paths that ever do, over the horizon
    probability_90d: float                 # ...within the first 90 days
    first_date_p50: date | None            # first day the median path is below buffer
    first_date_p10: date | None            # first day the bad-luck (p10) path is below buffer


class MonthlyNet(Base):
    mean: float                            # each path's average monthly change in liquid cash
    p10: float
    p90: float


ForecastEventKind = Literal["raise", "rent_renewal", "loan_ends", "cd_matures", "lump_sum", "income_change"]


class ForecastEvent(Base):
    day: date
    kind: ForecastEventKind
    description: str
    monthly_impact: float | None = None    # signed $/mo effect on cash flow; None when there is no cash effect (cd_matures)


class Assumption(Base):
    value: float
    source: str
    as_of: str


class Backtest(Base):
    """Rebuilt from the first 18 months only, forecast the rest, compared with the actual daily balance."""
    cutoff: date
    days: int                              # days compared
    avg_monthly_outflow: float             # average monthly checking outflow in the 18 training months; the yardstick below
    # Honest errors, as a share of one month's outflow (the stricter of the two is the one to quote):
    outflow_err_30d: float | None          # mean |p50 - actual| over the first N days / avg monthly outflow
    outflow_err_90d: float | None
    outflow_err_180d: float | None
    net_change_err_30d: float | None       # |forecast - actual cumulative net change| at day N / avg monthly outflow
    net_change_err_90d: float | None
    net_change_err_180d: float | None
    # Lenient: relative to the balance itself, which sits near zero for many customers
    mape_30d: float | None                 # mean |p50 - actual| / max(|actual|, 10% of monthly take-home); decimal
    mape_90d: float | None
    mape_180d: float | None
    band_coverage: float | None            # share of actual days inside p10-p90
    band_coverage_month_end: float | None = None   # same, month-end days only (the main metric: beyond 90 days the chart is monthly)
    known_one_off: str | None = None       # an unforecastable event in the test window (e.g. opened a CD); left out of calibration


class ForecastChange(Base):
    """A what-if applied to the schedule / starting balance. Only lump_sum and income_change are implemented."""
    type: Literal["lump_sum", "extra_monthly", "add_loan", "income_change", "expense_change"]
    amount: float                          # lump_sum: signed cash on `day`; income_change: signed change in monthly take-home from `day`
    day: date | None = None                # default: the day after as_of
    description: str = ""


class LiquidForecast(Base):
    profile_id: str
    as_of_date: date
    horizon_months: int
    paths: int
    start_liquid: float
    daily: PercentileBands | None          # days 0-90 (91 values); None in the summary version
    monthly: list[MonthEndPoint] | None    # month-end balances; None in the summary version
    low_point: LowPoint
    overdraft_risk: Shortfall              # buffer $0: a true emergency, drives the urgent alert
    cushion_risk: Shortfall                # buffer = one month of essential expenses: the default risk indicator ("below a safe cushion in March")
    monthly_net: MonthlyNet
    drivers: dict[str, float]              # month 1 scheduled cash by kind, plus variable_spend_median and interest (signed)
    events: list[ForecastEvent]
    assumptions: dict[str, Assumption]     # every assumption used
    backtest: Backtest | None = None
    calibration: str | None = None         # set when the p10-p90 bands were scaled using backtest errors (and how well that held up)

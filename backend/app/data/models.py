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
]
LIABILITY_TYPES: frozenset[str] = frozenset({"credit_card", "auto_loan", "personal_loan", "mortgage"})
LIQUID_TYPES: frozenset[str] = frozenset({"checking", "savings"})
INSTALLMENT_TYPES: frozenset[str] = frozenset({"auto_loan", "personal_loan", "mortgage"})

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
    def utilization(self) -> float | None:
        if self.type == "credit_card" and self.credit_limit:
            return self.balance / self.credit_limit
        return None


class Recurring(Base):
    """A cash flow that repeats (derived from checking transactions)."""
    description: str
    category: str
    monthly_amount: float                  # signed: + inflow, - outflow; normalized to per-month
    amount_per_occurrence: float           # signed
    occurrences_per_month: float = 1.0     # biweekly pay ~2.17
    typical_day: int = Field(ge=1, le=31)
    months_seen: int = Field(ge=1, le=24)
    account_id: str


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
    target_age: int = Field(67, ge=50, le=80)
    ira_type: Literal["roth", "traditional", ""] = ""


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

    # money
    accounts: list[Account]
    recurring: list[Recurring] = Field(default_factory=list)
    variable_spend: dict[str, float] = Field(default_factory=dict)  # category -> avg monthly $ (non-recurring)
    history: list[MonthPoint] = Field(default_factory=list)
    holdings: list[Holding] = Field(default_factory=list)
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

"""Response models that are not already in data/models.py."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..data.models import Base


class LLMHealth(Base):
    reachable: bool                      # the LLM server answered GET /models
    models_ready: bool                   # every configured local model is loaded


class Health(Base):
    status: str
    profiles_loaded: int
    as_of: str
    data_hash: str                       # changes whenever the data or the code that builds profiles changes
    llm: LLMHealth


class ProfileListItem(Base):
    id: str
    age: int
    employment: str
    housing: str
    credit_tier: str
    net_worth: float
    monthly_surplus: float
    flags: list[str]                     # featured, paycheck_to_paycheck, revolver, has_mortgage


class Transaction(Base):
    transaction_id: str
    account_id: str
    account_type: str
    posted_date: str                     # ISO date
    transaction_type: str
    category: str
    description: str
    amount: float
    balance_change: float
    balance_after: float
    flow_class: str
    transfer_id: str | None = None


class TransactionPage(Base):
    items: list[Transaction]
    total: int
    page: int
    page_size: int


class FormulaInfo(Base):
    name: str
    category: str
    description: str
    args_schema: dict[str, Any]          # JSON schema of the arguments: also the tool schema the agents will see
    result_schema: dict[str, Any] | None = None   # JSON schema of what it returns


class AssumptionsResponse(Base):
    assumptions: dict[str, dict[str, Any]]    # assumptions.json: {name: {value, source, as_of}}
    calibration: str | None                   # how the forecast bands were calibrated, and how well that held out
    calibration_details: dict[str, Any] | None = None

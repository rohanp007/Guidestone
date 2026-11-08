"""Event registry: what each user-facing event computes, which tools the planner may add, and how its result is cached."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..data.models import Profile
from .models import PlanStep


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class EventSpec:
    name: str
    feature: str                                         # which screen the result feeds
    params: type[BaseModel]                              # validated request body
    precompute: list[PlanStep]                           # formulas always run, no LLM
    allowed_tools: list[str]                             # what the planner may add
    default_plan: Callable[[Profile], list[PlanStep]]    # used when planning fails; decided in code from the profile
    explain: bool                                        # produce a grounded summary
    advise: bool                                         # run the decision rules and the advisor
    cache: Literal["session", "params", "none"]          # session / params: keyed by the params, kept until the data or prompt changes
    pipeline: Literal["dashboard", "payoff"] = "dashboard"

    def tools_for(self, profile: Profile) -> list[str]:
        """The allowed tools that make sense for this customer (a retiree has no paycheck to lose)."""
        return [t for t in self.allowed_tools if not (t == "job_loss_stress" and profile.employment == "retired")]


def _dashboard_default(profile: Profile) -> list[PlanStep]:
    if profile.employment == "retired":
        return []
    return [PlanStep(tool="job_loss_stress", args={"horizon_months": 24}, reason="default: emergency fund and job-loss check")]


class SimulatePayoffParams(BaseModel):
    """What the Pay Off screen sends. Omitted = the plan's default (the monthly surplus, the safe lump sum, the recommended strategy);
    0 = none."""
    model_config = ConfigDict(extra="forbid")
    extra_monthly: float | None = Field(None, ge=0, le=1_000_000)
    lump_sum: float | None = Field(None, ge=0, le=100_000_000)
    strategy: Literal["A", "B", "C", "D", "E"] | None = None


EVENTS: dict[str, EventSpec] = {
    "dashboard_open": EventSpec(
        name="dashboard_open", feature="dashboard", params=NoParams,
        precompute=[PlanStep(tool="forecast_liquid_summary", args={"horizon_months": 24}, reason="precompute"),
                    PlanStep(tool="paycheck_to_paycheck", reason="precompute")],
        allowed_tools=["job_loss_stress", "forecast_liquid_summary"],
        default_plan=_dashboard_default, explain=True, advise=True, cache="session"),
    # no planner: the plan is fixed (payoff_strategies_summary, then simulate_payoff for the recommended strategy)
    "simulate_payoff": EventSpec(
        name="simulate_payoff", feature="payoff", params=SimulatePayoffParams, precompute=[], allowed_tools=[],
        default_plan=lambda profile: [], explain=False, advise=False, cache="params", pipeline="payoff"),
}


def get_event(name: str) -> EventSpec:
    """KeyError for an unknown event; the API turns it into a 404."""
    return EVENTS[name]

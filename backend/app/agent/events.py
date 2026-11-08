"""Event registry: what each user-facing event computes, which tools the planner may add, and how its result is cached."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

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

    def tools_for(self, profile: Profile) -> list[str]:
        """The allowed tools that make sense for this customer (a retiree has no paycheck to lose)."""
        return [t for t in self.allowed_tools if not (t == "job_loss_stress" and profile.employment == "retired")]


def _dashboard_default(profile: Profile) -> list[PlanStep]:
    if profile.employment == "retired":
        return []
    return [PlanStep(tool="job_loss_stress", args={"horizon_months": 24}, reason="default: emergency fund and job-loss check")]


EVENTS: dict[str, EventSpec] = {
    "dashboard_open": EventSpec(
        name="dashboard_open", feature="dashboard", params=NoParams,
        precompute=[PlanStep(tool="forecast_liquid_summary", args={"horizon_months": 24}, reason="precompute"),
                    PlanStep(tool="paycheck_to_paycheck", reason="precompute")],
        allowed_tools=["job_loss_stress", "forecast_liquid_summary"],
        default_plan=_dashboard_default, explain=True, advise=True, cache="session"),
}


def get_event(name: str) -> EventSpec:
    """KeyError for an unknown event; the API turns it into a 404."""
    return EVENTS[name]

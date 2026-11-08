"""Formulas as model tools, bound to one customer's profile."""
from __future__ import annotations

from pydantic import ConfigDict, create_model

from ..data.models import Profile
from ..formulas import forecast, health, rules, stress  # noqa: F401  (importing registers the formulas)
from ..formulas.registry import FORMULAS
from .client import Tool


# The arguments an agent may set, per formula. The full schema (nested what-if definitions, seeds, path counts) costs hundreds of prompt
# tokens on every call and invites invented arguments; anything left out takes the formula's default.
AGENT_ARGS: dict[str, tuple[str, ...]] = {"forecast_liquid_summary": ("horizon_months",), "job_loss_stress": ("horizon_months", "target_months")}


def session_tool(profile: Profile, name: str, args: tuple[str, ...] | None = None) -> Tool:
    """The formula `name` bound to `profile`: the model supplies the arguments and never chooses whose data it reads.

    Where a `<name>_summary` variant exists, agents must use it (the full result carries daily / monthly arrays that would flood the prompt)."""
    if not name.endswith("_summary") and f"{name}_summary" in FORMULAS:
        raise ValueError(f"agents must use {name}_summary, not {name}")
    f = FORMULAS[name]
    keep = args if args is not None else AGENT_ARGS.get(name)
    model = f.args
    if keep is not None:
        model = create_model(f"{f.args.__name__}Slim", __config__=ConfigDict(extra="forbid"),
                             **{k: (f.args.model_fields[k].annotation, f.args.model_fields[k]) for k in keep})
    return Tool(f.name, f.description, model, lambda **kw: f.fn(profile, **kw))

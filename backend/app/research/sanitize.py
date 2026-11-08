"""What the Researcher may be told. An allowlist: the segment, the budget rounded DOWN to a $5k band, and the priorities. Nothing from
the profile ever reaches it, and anything else in the input is dropped."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, BaseModel

from . import pack

BAND = 5_000
Priority = Literal["price", "mpg", "safety"]
PRIORITIES = ("price", "mpg", "safety")


class BuyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    segment: Literal["compact_suv", "midsize_sedan"]
    budget_band: int | None = None            # dollars, a multiple of 5,000
    priorities: tuple[Priority, ...] = ()


def band(budget: Any) -> int | None:
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget != budget or budget < 0:
        return None
    return int(budget // BAND) * BAND


def sanitize(raw: dict[str, Any]) -> BuyQuery:
    """Only `segment`, `budget` and `priorities` are read; every other key, whatever it holds, is ignored. A bad segment is an error."""
    segment = raw.get("segment") if isinstance(raw, dict) else None
    if segment not in pack.SEGMENTS:
        raise ValueError(f"segment must be one of {pack.SEGMENTS}")
    prios = raw.get("priorities") or []
    seen: list[str] = []
    for p in prios if isinstance(prios, (list, tuple)) else []:
        if p in PRIORITIES and p not in seen:
            seen.append(p)
    return BuyQuery(segment=segment, budget_band=band(raw.get("budget")), priorities=tuple(seen))

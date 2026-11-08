"""The Buy research pack (cars.json): cited figures only. Loading, eligibility, and the auto-loan APR lookup (code only, never shown to a model)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

PACK_PATH = Path(__file__).with_name("cars.json")
SEGMENTS = ("compact_suv", "midsize_sedan")
FIGURES = ("msrp", "mpg_combined", "nhtsa_stars", "iihs_award")


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(PACK_PATH.read_text())


def vehicles(segment: str | None = None) -> list[dict]:
    return [v for v in load()["vehicles"] if segment is None or v["segment"] == segment]


def vehicle(vehicle_id: str) -> dict | None:
    return next((v for v in load()["vehicles"] if v["id"] == vehicle_id), None)


def price(v: dict) -> float | None:
    """Starting MSRP, or None when the pack has no cited price (such a vehicle cannot be priced, so it is not offered)."""
    return v["msrp"]["value"]


def eligible(segment: str, budget_band: int | None) -> list[dict]:
    """Vehicles in the segment with a cited price at or under the (rounded-down) budget."""
    return [v for v in vehicles(segment) if price(v) is not None and (budget_band is None or price(v) <= budget_band)]


def apr_for_score(score: int, condition: str = "new") -> tuple[float, dict]:
    """(APR as a fraction, the tier row) for a credit score. A score under the lowest tier uses the lowest tier."""
    tiers = load()["auto_apr_by_tier"]
    row = next((t for t in tiers if t["score_min"] <= score <= t["score_max"]), tiers[-1] if score < tiers[-1]["score_min"] else tiers[0])
    return row[f"apr_{condition}"]["value"] / 100, row


def source_line(record: dict) -> dict:
    """What the UI shows under a figure: title and date, with the url as a link."""
    return {"source_title": record["source_title"], "fetched_at": record["fetched_at"], "url": record["url"]}

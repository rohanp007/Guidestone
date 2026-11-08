"""The Researcher's input: an allowlist. Nothing from the customer's profile can get through."""
from __future__ import annotations

import pytest

from app.data.loader import load_profiles
from app.research.sanitize import BuyQuery, band, sanitize


def test_the_budget_rounds_down_to_a_5k_band():
    assert [band(x) for x in (0, 4_999, 5_000, 32_999, 35_000.5, 1e9)] == [0, 0, 5_000, 30_000, 35_000, 1_000_000_000]
    assert [band(x) for x in (None, -1, "30000", True, float("nan"))] == [None] * 5


def test_only_segment_budget_and_priorities_pass():
    q = sanitize({"segment": "compact_suv", "budget": 33_000, "priorities": ["safety", "price", "safety", "color", 7], "choice_id": "honda_crv"})
    assert q.model_dump() == {"segment": "compact_suv", "budget_band": 30_000, "priorities": ("safety", "price")}


def test_no_customer_field_can_pass():
    p = load_profiles()["C095"]
    raw = {"segment": "midsize_sedan", "budget": 41_234.56, "priorities": ["mpg"], **p.model_dump(mode="json"),
           "customer_id": p.id, "credit_score": p.credit_score, "gross_monthly": p.gross_monthly, "liquid_cash": 67_902.28, "apr": 0.0615}
    q = sanitize(raw)
    assert set(q.model_dump()) == {"segment", "budget_band", "priorities"} and q.budget_band == 40_000
    wire = q.model_dump_json()
    for secret in (p.id, str(p.credit_score), str(int(p.gross_monthly)), "67902", "0.0615", p.state, "owner_with_mortgage"):
        assert secret not in wire
    with pytest.raises(Exception):
        BuyQuery(segment="compact_suv", credit_score=712)                      # the model refuses extra fields
    with pytest.raises(Exception):
        BuyQuery(segment="compact_suv").budget_band = 1                          # and is frozen


@pytest.mark.parametrize("bad", [None, "", "truck", "COMPACT_SUV", 5])
def test_a_bad_segment_is_an_error(bad):
    with pytest.raises(ValueError):
        sanitize({"segment": bad})

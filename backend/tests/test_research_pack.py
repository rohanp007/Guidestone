"""The Buy research pack: every figure is cited, and nothing is claimed without the page text to back it."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

PACK = json.loads((Path(__file__).resolve().parent.parent / "app" / "research" / "cars.json").read_text())
FIGURES = ("msrp", "mpg_combined", "nhtsa_stars", "iihs_award")


def records():
    for v in PACK["vehicles"]:
        for f in FIGURES:
            yield f"{v['id']}.{f}", v[f]
    for t in PACK["auto_apr_by_tier"]:
        for f in ("apr_new", "apr_used"):
            yield f"{t['tier']}.{f}", t[f]


def squash(s) -> str:
    return re.sub(r"[\s,]", "", str(s)).lower().replace("+", "")


def contains_value(snippet: str, value) -> bool:
    s = squash(snippet)
    forms = {squash(value)}
    if isinstance(value, float):
        forms |= {squash(f"{value:g}"), squash(f"{value:.2f}")}
    return any(f in s for f in forms)


def test_shape_and_coverage():
    assert PACK["generated_at"] == "2026-11-08"
    assert {v["segment"] for v in PACK["vehicles"]} == {"compact_suv", "midsize_sedan"} and len(PACK["vehicles"]) == 6
    assert [t["tier"] for t in PACK["auto_apr_by_tier"]] == ["super_prime", "prime", "near_prime", "subprime", "deep_subprime"]
    for t in PACK["auto_apr_by_tier"]:
        assert t["score_min"] < t["score_max"]


def test_every_non_null_figure_is_cited_and_its_snippet_holds_the_value():
    seen = 0
    for name, r in records():
        if r["value"] is None:
            assert r["note"], f"{name}: a null needs the reason"
            continue
        seen += 1
        assert r["url"] and r["url"].startswith("https://"), name
        assert r["fetched_at"] and date.fromisoformat(r["fetched_at"]) <= date(2026, 11, 8), name
        assert r["snippet"] and len(r["snippet"].split()) <= 25, name
        assert contains_value(r["snippet"], r["value"]), (name, r["value"], r["snippet"])
        assert r["source_title"] and r["unit"], name
    assert seen >= 20


def test_no_figure_is_marked_real_without_a_url_and_a_snippet():
    for name, r in records():
        if r["illustrative"] is False:
            assert r["url"] and r["snippet"] and r["value"] is not None, name
        if r["value"] is None:
            assert r["illustrative"] is True, name           # nothing verified


def test_apr_tiers_cover_new_and_used_in_order():
    tiers = PACK["auto_apr_by_tier"]
    for a, b in zip(tiers, tiers[1:]):
        assert a["score_min"] > b["score_max"] and a["apr_new"]["value"] < b["apr_new"]["value"] and a["apr_used"]["value"] < b["apr_used"]["value"]
        assert a["apr_used"]["value"] > a["apr_new"]["value"]

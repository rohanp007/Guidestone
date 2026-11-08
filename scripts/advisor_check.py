#!/usr/bin/env python3
"""Live check of the advisor inside dashboard_open on the five golden personas (needs the LLM servers). Prints the verdict (set by
code), each move with its source and text, grounding, and the timings. Exits non-zero when a rule is broken.

Run:  .venv/bin/python3 scripts/advisor_check.py [C002 ...]          (from the repo root)
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.agent import cache, orchestrator              # noqa: E402
from app.data.loader import load_profiles              # noqa: E402
from app.llm import client                             # noqa: E402

GOLDEN = ["C002", "C067", "C095", "C010", "C059"]


async def main(ids) -> int:
    profiles, bad = load_profiles(), []
    cache.clear()
    client.prewarm()
    for cid in ids:
        t0 = time.perf_counter()
        r = await orchestrator.run("dashboard_open", profiles[cid], {}, refresh=True)
        wall = time.perf_counter() - t0
        a = r.advice
        steps = {s.kind: s for s in r.trace if s.kind in ("rules", "advise", "explain")}
        adv_calls = [e for e in r.llm_trace if e.get("agent") == "advisor"]
        print(f"\n===== {cid}: verdict {a.verdict} (current step {a.current_step}), advisor source {a.source}, grounded {a.grounding.ok} "
              f"({a.grounding.checked} figures, retried={a.grounding.retried}), degraded={r.degraded}")
        for m in a.moves:
            print(f"  {m.rank}. [{m.source}] {m.move_code}: {m.title_text}\n       {m.why_text}")
        for n in a.notes:
            print(f"  note: {n}")
        print(f"  timings: total {wall:.1f} s | rules {steps['rules'].ms:.0f} ms | advisor {steps['advise'].ms / 1000:.1f} s "
              f"({len(adv_calls)} model call(s), {sum(e['completion_tokens'] for e in adv_calls)} completion tokens) | explain {steps['explain'].ms / 1000:.1f} s")
        codes = {m["move_code"] for m in r.compute["candidate_moves"]["moves"]}
        if not {m.move_code for m in a.moves} <= codes: bad.append(f"{cid}: move not in candidates")
        if len(a.moves) > 3: bad.append(f"{cid}: more than 3 moves")
    print("\nFAIL: " + "; ".join(bad) if bad else "\nOK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or GOLDEN)))

#!/usr/bin/env python3
"""Live check of the simulate_payoff event on the golden personas (needs the advisor's LLM server): verdict, strategy, the answer text,
its source, timings. Run:  .venv/bin/python3 scripts/payoff_check.py [C067 ...]          (from the repo root)"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.agent import cache, orchestrator              # noqa: E402
from app.data.loader import load_profiles              # noqa: E402
from app.llm import client                             # noqa: E402

GOLDEN = ["C067", "C059", "C095", "C002", "C010"]


async def main(ids) -> int:
    profiles = load_profiles()
    cache.clear()
    client.prewarm()
    for cid in ids:
        t0 = time.perf_counter()
        r = await orchestrator.run("simulate_payoff", profiles[cid], {})
        wall = time.perf_counter() - t0
        p = r.payoff
        d = p.answer.diagnostics
        print(f"\n===== {cid}: verdict {p.verdict}, plan {p.strategy_code} ({p.strategy_title}), answer from {p.answer.source}, "
              f"first_try_ok={d.first_try_ok} retried={d.retried} reasons={d.first_reasons or '-'}, degraded={r.degraded}")
        print(f"  {p.answer.text}")
        print(f"  extra {p.extra_monthly:,.0f}/mo, lump {p.lump_sum:,.0f} (safe {p.safe_lump_sum:,.0f}); compared {[c.code for c in p.comparison]}; "
              f"simulation {'yes' if p.simulation else 'no'}; {wall:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or GOLDEN)))

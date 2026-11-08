#!/usr/bin/env python3
"""Live check of the orchestrator's dashboard_open on the featured personas (needs the LLM servers). Prints only the planned tools
and rejection codes, status, the summary text and source, and times (uncached, then cached). Exits non-zero if a rule is broken.

Run:  .venv/bin/python3 scripts/agent_check.py          (from the repo root)
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.agent import cache, orchestrator              # noqa: E402
from app.config import FEATURED_PERSONAS               # noqa: E402
from app.data.loader import load_profiles              # noqa: E402
from app.llm import client                             # noqa: E402

MAX_UNCACHED_S, MAX_CACHED_MS = 5.0, 50.0


async def main() -> int:
    profiles, bad = load_profiles(), []
    cache.clear()
    client.prewarm()                                        # as the server does at startup
    for cid in FEATURED_PERSONAS:
        p = profiles[cid]
        t0 = time.perf_counter(); r = await orchestrator.run("dashboard_open", p, {}); cold = time.perf_counter() - t0
        t0 = time.perf_counter(); hit = await orchestrator.run("dashboard_open", p, {}); warm = (time.perf_counter() - t0) * 1000
        planned = [s.name for s in r.trace if s.kind == "tool"]
        rejected = [f"{s.name}:{s.detail['code']}" for s in r.trace if s.kind == "plan_rejected"]
        fallbacks = [s.name for s in r.trace if s.kind == "fallback"]
        print(f"{cid} ({p.employment}): plan={planned or '-'} rejected={rejected or '-'} fallbacks={fallbacks or '-'} status={r.status} "
              f"source={r.summary.source} grounded={r.summary.grounding.ok}")
        print(f"   {r.summary.text}")
        print(f"   {cold:.1f} s uncached, {warm:.1f} ms cached, degraded={r.degraded}")
        if not r.summary.grounding.ok: bad.append(f"{cid}: summary not grounded")
        if cold > MAX_UNCACHED_S: bad.append(f"{cid}: {cold:.1f} s uncached")
        if warm > MAX_CACHED_MS or not orchestrator.is_cache_hit(hit): bad.append(f"{cid}: cached call {warm:.0f} ms")
        if p.employment == "retired" and planned: bad.append(f"{cid}: retiree planned {planned}")
        if cid in ("C002", "C067") and "job_loss_stress" not in planned: bad.append(f"{cid}: did not plan job_loss_stress")
    print("FAIL: " + "; ".join(bad) if bad else "OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

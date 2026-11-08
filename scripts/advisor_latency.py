#!/usr/bin/env python3
"""Advisor latency and quality in both modes, on the five golden personas (needs both LLM servers).

  reasoning-on   LM Studio :1234, nvidia/nemotron-3-nano-4b, reasoning on (the documented option)
  reasoning-off  llama-server :1235, nemotron-nano-4b-nothink, reasoning off (the advisor's default)

For each persona and mode, N runs of the advisor with NO time budget (so this measures the model, not the budget). Reports latency,
how often the model's first answer passed every check, retries, moves served from the template, and the reason codes of the checks
that failed. It never prints model text or reasoning: only counts, times and codes. It does not change any configuration.

Run:  .venv/bin/python3 scripts/advisor_latency.py [--runs 3] [--modes reasoning-off] [C002 ...]          (from the repo root)
"""
import argparse, asyncio, collections, math, os, statistics, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.agent import advisor                           # noqa: E402
from app.data.loader import load_profiles               # noqa: E402
from app.formulas import forecast as fc                 # noqa: E402
from app.llm import client                              # noqa: E402
from app.llm.facts import forecast_facts                # noqa: E402

GOLDEN = ["C002", "C067", "C095", "C010", "C059"]
BUDGET_S = 12.0
MODES = {
    "reasoning-on": {"LLM_ADVISOR_BACKEND": "lmstudio", "LLM_ADVISOR_BASE_URL": "http://localhost:1234/v1",
                     "LLM_ADVISOR_MODEL": "nvidia/nemotron-3-nano-4b", "LLM_ADVISOR_REASONING": "true", "LLM_ADVISOR_TIMEOUT": "120"},
    "reasoning-off": {"LLM_ADVISOR_BACKEND": "llamacpp", "LLM_ADVISOR_BASE_URL": "http://localhost:1235/v1",
                      "LLM_ADVISOR_MODEL": "nemotron-nano-4b-nothink", "LLM_ADVISOR_REASONING": "false"},
}


async def one_mode(mode, env, ids, runs, profiles, results):
    for k in [k for k in os.environ if k.startswith("LLM_ADVISOR_")]:
        del os.environ[k]
    os.environ.update(env)
    for cid in ids:
        facts = forecast_facts(fc.forecast_liquid(profiles[cid], horizon_months=24))
        facts["employment"] = profiles[cid].employment
        inp = advisor.prepare(profiles[cid], facts)
        for i in range(runs):
            t0 = time.perf_counter()
            a = await advisor.advise(inp, facts, budget_s=math.inf)
            results[(mode, cid)].append((time.perf_counter() - t0, a))
            d = a.diagnostics
            print(f"  {mode} {cid} run {i + 1}: {time.perf_counter() - t0:5.1f} s  first_try_ok={d.first_try_ok} retried={d.retried} "
                  f"template_moves={d.template_moves}/{len(a.moves)} reasons={d.first_reasons or '-'}"
                  + "".join(f"\n      bad reply: finish_reason={b.finish_reason} completion_tokens={b.completion_tokens} max_tokens={b.max_tokens}"
                            for b in d.bad_replies), flush=True)


def table(results, ids, runs):
    print(f"\n{'persona':<7} {'mode':<14} {'latency median (min-max)':<26} {'first-try ok':<13} {'retried':<8} {'template moves':<15} "
          f"{'over %ds':<9} first-try failure reasons" % BUDGET_S)
    for cid in ids:
        for mode in MODES:
            rs = results[(mode, cid)]
            if not rs:
                continue
            lat = [t for t, _ in rs]
            reasons = collections.Counter(r for _, a in rs for r in a.diagnostics.first_reasons)
            moves = sum(len(a.moves) for _, a in rs)
            print(f"{cid:<7} {mode:<14} {statistics.median(lat):5.1f} s ({min(lat):4.1f}-{max(lat):4.1f})".ljust(48)
                  + f" {sum(a.diagnostics.first_try_ok for _, a in rs)}/{len(rs):<11} {sum(a.diagnostics.retried for _, a in rs)}/{len(rs):<6} "
                  f"{sum(a.diagnostics.template_moves for _, a in rs)}/{moves:<13} {sum(t > BUDGET_S for t in lat)}/{len(rs):<7} "
                  + (", ".join(f"{k}x{v}" for k, v in reasons.most_common()) or "-"))
    print("\nunusable replies (llm_bad_reply), by how the reply ended:")
    for mode in MODES:
        ends = collections.Counter(b.finish_reason for cid in ids for _, a in results[(mode, cid)] for b in a.diagnostics.bad_replies)
        print(f"  {mode:<14} " + (", ".join(f"{k} x{v}" for k, v in ends.most_common()) or "none") + "   (length = truncated at max_tokens)")
    print()
    for mode in MODES:
        every = [r for cid in ids for r in results[(mode, cid)]]
        if every:
            lat = [t for t, _ in every]
            print(f"{mode:<14} all personas: median {statistics.median(lat):.1f} s, p90 {sorted(lat)[int(0.9 * (len(lat) - 1))]:.1f} s, "
                  f"first-try ok {sum(a.diagnostics.first_try_ok for _, a in every)}/{len(every)}, "
                  f"over the {BUDGET_S:.0f} s budget {sum(t > BUDGET_S for t in lat)}/{len(lat)}")


async def main(ids, runs, modes):
    profiles, results = load_profiles(), collections.defaultdict(list)
    saved = {k: v for k, v in os.environ.items() if k.startswith("LLM_ADVISOR_")}
    try:
        for mode in modes:
            env = MODES[mode]
            await one_mode(mode, env, ids, runs, profiles, results)
    finally:
        for k in [k for k in os.environ if k.startswith("LLM_ADVISOR_")]:
            del os.environ[k]
        os.environ.update(saved)
        await client.close_clients()
    table(results, ids, runs)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", default=GOLDEN)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--modes", nargs="+", choices=list(MODES), default=list(MODES))
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.ids, a.runs, a.modes)))

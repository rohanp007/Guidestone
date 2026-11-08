#!/usr/bin/env python3
"""Print the rule outputs for the golden personas (docs/rules.md section 5): the waterfall, the candidate moves and the payoff order.

Run:  .venv/bin/python3 scripts/show_rules.py [C002 C067 ...]          (from the repo root)
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.data.loader import load_profiles              # noqa: E402
from app.formulas import rules                         # noqa: E402

GOLDEN = ["C002", "C067", "C095", "C010", "C059"]


def money(x): return "-" if x is None else f"${x:,.0f}"


def main(ids):
    profiles = load_profiles()
    for cid in ids:
        p = profiles[cid]
        wf, mv, po = rules.priority_waterfall(p), rules.candidate_moves(p), rules.payoff_priority(p)
        print(f"\n===== {cid}  {p.employment}, age {p.age}, credit {p.credit_score}, surplus {money(wf.surplus)}/mo, current step: {wf.current_step}")
        for s in wf.steps:
            mark = ">>" if s.step == wf.current_step else "  "
            print(f"{mark} {s.step} {s.name:<36} {s.status:<14} target {money(s.target):>9}  current {money(s.current):>9}  gap {money(s.gap):>9}  "
                  f"-> {money(s.monthly_suggested):>6}/mo  [{s.reason_code}]")
        print(f"   unallocated surplus: {money(wf.unallocated)}")
        print("   candidate moves:", ", ".join(f"{m.move_code}(p{m.priority}{'*' if m.addresses_current_step else ''})" for m in mv.moves) or "none")
        print("   payoff order:", [(i.account_id.split('-', 2)[-1], f"{i.apr:.1%}", money(i.balance), i.reason_code) for i in po.order] or "none",
              "| safe lump", money(po.safe_lump_sum), "| low-cushion", money(po.safe_lump_sum_low_cushion), "| flags", po.flags)


if __name__ == "__main__":
    main(sys.argv[1:] or GOLDEN)

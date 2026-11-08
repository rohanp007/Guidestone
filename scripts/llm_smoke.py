#!/usr/bin/env python3
"""Smoke test for app/llm against a live LM Studio: C002 -> the model calls forecast_liquid_summary -> JSON summary.

Prints only the tool name + arguments, the parsed summary, latency and tokens. Exits non-zero on any failure.

Run:  .venv/bin/python3 scripts/llm_smoke.py          (from the repo root, LM Studio serving the configured model)
"""
import asyncio, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.data.loader import load_profiles                         # noqa: E402
from app.llm.client import LLMError, complete, tool_message   # noqa: E402
from app.llm.facts import forecast_facts, model_view, placeholders                         # noqa: E402
from app.llm.grounding import explain                         # noqa: E402
from app.llm.prompts import EXPLAIN_PROMPT                         # noqa: E402
from app.llm.tools import session_tool                         # noqa: E402

CUSTOMER = "C002"


def show(label: str, r) -> None:
    t = r.timings
    print(f"{label}: prompt {r.prompt_tokens} tok (cached {t.get('cache_n', 0):.0f}), completion {r.completion_tokens} tok, "
          f"server prompt_ms {t.get('prompt_ms', 0):.0f}, predicted_ms {t.get('predicted_ms', 0):.0f}, wall {r.latency_ms:.0f} ms")


async def main() -> int:
    tool = session_tool(load_profiles()[CUSTOMER], "forecast_liquid_summary")
    messages = [{"role": "system", "content": "You are a careful financial assistant. Use the tools to get numbers; never invent them."},
                {"role": "user", "content": "Forecast this customer's liquid balance for the next 12 months with the tool, "
                                            "then say when it is ready."}]
    first = await complete("orchestrator", messages, tools=[tool], tool_choice="required")
    if not first.tool_calls:
        print("FAIL: the model did not call a tool")
        return 1
    messages.append(first.assistant_message())
    for call in first.tool_calls:
        print(f"tool: {call.name} {json.dumps(call.arguments)}")
        if call.name != tool.name:
            print("FAIL: unknown tool"); return 1
        forecast = await tool.arun(call.arguments)                       # validates the arguments with the tool's model
        facts = forecast_facts(forecast)
        messages.append(tool_message(call, "Forecast ready."))           # the model never sees figures: it gets plain phrases and placeholders
    # same tools (tool_choice "none") keeps the prompt prefix identical to the first call, so the server can reuse its cache
    # the writing step is a separate call with its own prompt and the plain-language view of the facts
    out = await explain("orchestrator", [{"role": "system", "content": EXPLAIN_PROMPT}, {"role": "user", "content": model_view(facts)}],
                        facts, context_numbers=(12, 24), placeholders=placeholders(facts), max_tokens=160)
    print(f"status (set by code): {facts['overall_status']}")
    print(f"explanation: {out.text}")
    print(f"grounding: {len(out.report.numbers)} figures checked, {len(out.report.conflicts)} wording conflicts, retried={out.retried}, template={out.used_template}"
          f"{'  -> all figures verified' if out.report.ok else '  -> NOT VERIFIED'}")
    show("call 1", first)
    for i, c in enumerate(out.calls, 2): show(f"call {i}", c)
    calls = [first, *out.calls]
    print(f"latency: {sum(c.latency_ms for c in calls):.0f} ms   "
          f"tokens: {sum(c.prompt_tokens for c in calls)} prompt + {sum(c.completion_tokens for c in calls)} completion")
    return 0 if out.report.ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except LLMError as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        sys.exit(1)

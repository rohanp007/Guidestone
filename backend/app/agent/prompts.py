"""Orchestrator prompts. Versioned: PROMPT_VERSION is part of every cache key, so editing a prompt means bumping it."""
PROMPT_VERSION = "plan-v1.5/explain-v1.2"

# Short on purpose: the 4B model works best with brief instructions. The variable context goes last, in its own message,
# so the prompt prefix stays identical between runs and the server can reuse its cache.
# Not in the prompt on purpose: saying that {"steps":[]} is valid made the 4B model answer it every time (the code accepts it anyway).
ORCH_PLAN_V1 = """You choose extra financial calculations to run for a dashboard check-up.
You never calculate numbers. Choose only from the tools listed.
Add a tool when its result could change what the customer should do. Stress tests matter most for customers who look fine.
Return only JSON: {"steps":[{"tool":"<name>","args":{...},"reason":"<short phrase>"}]}, at most 4 steps.
Example for an employed customer: {"steps":[{"tool":"job_loss_stress","args":{"horizon_months":24},"reason":"check emergency fund coverage"}]}"""

EXPLAIN_V1 = ("You are a careful financial assistant. Write plain English: quote amounts the way they appear in the facts' display "
              "section, describe the status labels in ordinary words (not their names), and never say 'risk', 'safe', 'healthy' or "
              "'on track'. Keep it to two short sentences. ")

# When each tool is worth adding, shown to the planner next to the tool. The 4B model otherwise answers {"steps":[]} to everything.
TOOL_HINTS = {
    "job_loss_stress": "Run it for every employed customer: emergency-fund coverage and how long cash lasts if the paycheck stops.",
    "forecast_liquid_summary": "Already computed for 24 months. Add only for a different horizon.",
}

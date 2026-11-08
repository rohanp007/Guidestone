"""Prompt text shared by every agent."""

BASE_SYSTEM_PROMPT = "When you call a tool, output only the tool call, with no explanation."

FACTS_PROMPT = ("Explain why the numbers look the way they do, in 1-2 sentences, using only the facts: describe the status labels "
                "(cushion_status, trend, overdraft_status) as they are. Use only figures that appear in the facts. "
                "Do not state an overall judgment, risk level or reassurance. The app shows that separately.")

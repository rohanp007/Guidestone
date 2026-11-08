"""Fixed answer formats. Judgments (status, verdict, severity) are values chosen by code or checked by code; the model's prose only explains."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Explanation(BaseModel):
    explanation: str = Field(description="1-2 sentences that explain WHY, using only the facts")
    next_step_hint: str | None = Field(None, description="Optional short phrase, or null")

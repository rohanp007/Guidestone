"""Fixed answer formats. Judgments (status, verdict, severity) are values chosen by code or checked by code; the model's prose only explains."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Explanation(BaseModel):
    explanation: str = Field(description="One or two short sentences of plain English that explain why, using the placeholders for every figure")

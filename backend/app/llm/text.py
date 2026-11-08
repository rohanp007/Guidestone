"""Cleaning model output."""
from __future__ import annotations

import json
import re
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.S)


def strip_reasoning(text: str | None) -> str:
    """Remove <think> blocks, an unterminated trailing one, and a bare </think> (the opening tag is sometimes part of the prompt)."""
    text = _THINK.sub("", text or "")
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.split("<think>", 1)[0].strip()


def extract_json(text: str) -> Any:
    """First JSON object or array in the text, ignoring code fences and chatter around it."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"[{\[]", text):
        try:
            return dec.raw_decode(text[m.start():])[0]
        except ValueError:
            continue
    raise ValueError("no JSON found")

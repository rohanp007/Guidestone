"""App-wide settings."""
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"   # <repo>/data
AS_OF_DATE = date(2026, 10, 31)                           # last day of the generated history
FEATURED_PERSONAS: list[str] = ["C002", "C067", "C095", "C010", "C059"]                         # customer ids pinned for the demo

ASSUMPTIONS_PATH = Path(__file__).with_name("assumptions.json")


@lru_cache(maxsize=1)
def assumptions() -> dict[str, dict]:
    """Forecast assumptions, each {value, source, as_of}. Edit assumptions.json, not code."""
    return json.loads(ASSUMPTIONS_PATH.read_text())

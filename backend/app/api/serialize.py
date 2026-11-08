"""JSON-safe conversion. FastAPI crashes (or emits invalid JSON) on numpy / pandas values and NaN; everything that leaves the
API goes through `to_plain` first."""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def to_plain(obj: Any) -> Any:
    """Plain Python: dict / list / str / int / float / bool / None. NaN, NaT and +-inf become None; dates become ISO strings."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, BaseModel):
        return to_plain(obj.model_dump(mode="json"))
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.floating, float, Decimal)):
        x = float(obj)
        return x if math.isfinite(x) else None
    if obj is pd.NaT or obj is pd.NA:
        return None
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    if isinstance(obj, np.ndarray):
        return [to_plain(x) for x in obj.tolist()]
    if isinstance(obj, pd.DataFrame):
        return [to_plain(r) for r in obj.to_dict("records")]
    if isinstance(obj, pd.Series):
        return [to_plain(x) for x in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_plain(x) for x in obj]
    return str(obj)


class SafeJSONResponse(JSONResponse):
    """The app's default response class: sanitizes the content, then renders strict JSON (no NaN)."""

    def render(self, content: Any) -> bytes:
        return json.dumps(to_plain(content), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")

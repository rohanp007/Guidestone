from __future__ import annotations

from fastapi import APIRouter

from .. import config
from ..data import loader
from ..formulas import forecast
from .schemas import AssumptionsResponse, Health

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", profiles_loaded=len(loader.load_profiles()), as_of=config.AS_OF_DATE.isoformat(),
                  data_hash=loader.cache_key()[:12])


@router.get("/assumptions", response_model=AssumptionsResponse)
def assumptions() -> AssumptionsResponse:
    """For the "Assumptions we used" panel: every forecast assumption with its source, and how the bands were calibrated."""
    cal = forecast._calibration()
    return AssumptionsResponse(assumptions=config.assumptions(), calibration=cal["note"] if cal else None,
                               calibration_details={k: cal[k] for k in ("nominal", "anchors_days", "daily_factors", "month_end_factors",
                                                                         "customers", "left_out", "held_out", "backtest_error")} if cal else None)

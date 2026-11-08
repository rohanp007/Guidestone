from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from ..data.models import Profile
from ..formulas import forecast, health, rules, stress  # noqa: F401  (importing registers the formulas)
from ..formulas.registry import FORMULAS
from ..auth import current_user
from .deps import get_profile
from .schemas import FormulaInfo
from .serialize import SafeJSONResponse, to_plain

log = logging.getLogger("guidestone.formulas")
router = APIRouter(prefix="/api", tags=["formulas"])

# Formulas are deterministic (forecasts use fixed seeds), so a result depends only on (profile, formula, arguments).
_CACHE: dict[tuple[str, str, str], dict] = {}


@router.get("/formulas", response_model=list[FormulaInfo], dependencies=[Depends(current_user)])
def list_formulas() -> list[FormulaInfo]:
    """Every registered formula. `args_schema` is the Pydantic JSON schema of its arguments: the debug page builds forms from it
    and the agents will see it as the tool schema."""
    return [FormulaInfo(name=f.name, category=f.category, description=f.description, args_schema=f.args.model_json_schema(),
                        result_schema=f.result.model_json_schema(mode="serialization") if f.result else None)
            for f in sorted(FORMULAS.values(), key=lambda f: (f.category, f.name))]


@router.post("/profiles/{id}/formulas/{name}")
async def run_formula(name: str, request: Request, profile: Profile = Depends(get_profile)):
    formula = FORMULAS.get(name)                      # the registry is the only way a formula is ever looked up
    if formula is None:
        raise HTTPException(status_code=404, detail=f"formula {name} not found")
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
        args = formula.args.model_validate(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="request body is not valid JSON")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False, include_context=False)))

    key = (profile.id, name, json.dumps(args.model_dump(mode="json"), sort_keys=True))
    started = time.perf_counter()
    cached = _CACHE.get(key)
    hit = cached is not None
    if cached is None:
        try:
            result = formula.fn(profile, **{field: getattr(args, field) for field in type(args).model_fields})
            cached = _CACHE[key] = to_plain(result)
        except NotImplementedError as exc:
            return SafeJSONResponse({"detail": str(exc) or "not implemented", "formula": name}, status_code=501)
        except ValueError as exc:                     # a valid-looking argument the formula cannot use (e.g. a date outside the window)
            return SafeJSONResponse({"detail": str(exc), "formula": name}, status_code=422)
        except Exception as exc:                      # log the formula and the error type only: nothing customer-related
            log.error("formula %s failed: %s", name, type(exc).__name__)
            return SafeJSONResponse({"detail": "formula failed", "formula": name}, status_code=500)
    ms = 1000 * (time.perf_counter() - started)
    return SafeJSONResponse(cached, headers={"X-Compute-Ms": f"{ms:.1f}", "X-Cache": "hit" if hit else "miss"})

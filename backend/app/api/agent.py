"""POST /api/me/agent/{event}: run an orchestrator event for the signed-in customer. The customer is never a parameter."""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from ..agent import orchestrator
from ..agent.cache import RunSuperseded
from ..agent.events import EVENTS
from ..auth import User, current_user
from ..data.loader import load_profiles
from .serialize import SafeJSONResponse

log = logging.getLogger("guidestone.agent")
router = APIRouter(prefix="/api", tags=["agent"])


@router.post("/me/agent/{event}")
async def run_event(event: str, request: Request, refresh: int = 0, user: User = Depends(current_user)):
    if event not in EVENTS:
        raise HTTPException(status_code=404, detail=f"event {event} not found")
    profile = load_profiles().get(user.customer_id)        # whoever the session says; there is no way to name another customer
    if profile is None:
        raise HTTPException(status_code=403, detail="forbidden")
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    started = time.perf_counter()
    try:
        result = await orchestrator.run(event, profile, body, refresh=bool(refresh))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False, include_context=False, include_input=False)))
    except RunSuperseded:
        raise HTTPException(status_code=409, detail="superseded by a newer run")
    except TimeoutError:
        raise HTTPException(status_code=504, detail="the check-up took too long")
    return SafeJSONResponse(result.model_dump(mode="json"), headers={
        "X-Run-Ms": f"{1000 * (time.perf_counter() - started):.0f}", "X-Cache": "hit" if orchestrator.is_cache_hit(result) else "miss"})

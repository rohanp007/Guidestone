"""POST /api/me/agent/{event}: run an orchestrator event for the signed-in customer. The customer is never a parameter."""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from ..agent import orchestrator
from ..agent.cache import RunSuperseded
from ..agent.models import AnalysisResult
from ..agent.events import EVENTS
from ..agent.stream import StreamEvent
from ..auth import User, current_user
from ..data.loader import load_profiles
from .serialize import SafeJSONResponse, to_plain

log = logging.getLogger("guidestone.agent")
router = APIRouter(prefix="/api", tags=["agent"])


def _profile(user: User):
    profile = load_profiles().get(user.customer_id)        # whoever the session says; there is no way to name another customer
    if profile is None:
        raise HTTPException(status_code=403, detail="forbidden")
    return profile


async def _body(request: Request) -> dict:
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    return body


@router.post("/me/agent/{event}", response_model=AnalysisResult, responses={
    409: {"description": "A newer run for the same event replaced this one. Expected: ignore it and use the newer run's result."},
    504: {"description": "The run took too long before anything could be returned."}})
async def run_event(event: str, request: Request, refresh: int = 0, user: User = Depends(current_user)):
    if event not in EVENTS:
        raise HTTPException(status_code=404, detail=f"event {event} not found")
    profile = _profile(user)
    body = await _body(request)
    started = time.perf_counter()
    try:
        result = await orchestrator.run(event, profile, body, refresh=bool(refresh))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False, include_context=False, include_input=False)))
    except orchestrator.ChoiceNotInPicks:
        raise HTTPException(status_code=422, detail="choice_id is not one of the cars offered for this query")
    except RunSuperseded:
        raise HTTPException(status_code=409, detail="superseded by a newer run")
    except TimeoutError:
        raise HTTPException(status_code=504, detail="the check-up took too long")
    return SafeJSONResponse(result.model_dump(mode="json"), headers={
        "X-Run-Ms": f"{1000 * (time.perf_counter() - started):.0f}", "X-Cache": "hit" if orchestrator.is_cache_hit(result) else "miss"})


def _sse(ev: StreamEvent) -> bytes:
    """One server-sent event. Progress events leave out unset fields; the result event carries the AnalysisResult exactly as the plain endpoint does."""
    data = ev.model_dump(mode="json", exclude={"result"}, exclude_none=True)
    if ev.result is not None:
        data["result"] = ev.result.model_dump(mode="json")
    return f"event: {ev.type}\ndata: {json.dumps(to_plain(data), ensure_ascii=False, allow_nan=False, separators=(',', ':'))}\n\n".encode()


@router.post("/me/agent/{event}/stream", response_class=StreamingResponse, responses={
    200: {"model": StreamEvent, "description": "text/event-stream. One StreamEvent per `data:` line (event: progress), then a final event of type "
                                              "result (the full AnalysisResult) or error. A cached result replays its stored steps with detail.cached; "
                                              "a run in flight is joined."}})
async def run_event_stream(event: str, request: Request, refresh: int = 0, user: User = Depends(current_user)):
    """Same body and result as the plain endpoint, with the run's progress as server-sent events (plain-language labels only)."""
    if event not in EVENTS:
        raise HTTPException(status_code=404, detail=f"event {event} not found")
    profile = _profile(user)
    body = await _body(request)
    try:
        events = await orchestrator.open_stream(event, profile, body, refresh=bool(refresh))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False, include_context=False, include_input=False)))

    async def body_iter():
        async for ev in events:
            yield _sse(ev)

    return StreamingResponse(body_iter(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

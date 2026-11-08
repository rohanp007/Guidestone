"""In-memory result cache and one-run-at-a-time per (customer, event)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

MAX_ENTRIES = 500
_CACHE: "OrderedDict[tuple, Any]" = OrderedDict()
_EXPIRES: dict[tuple, float] = {}        # key -> time.monotonic() after which the entry is gone (only entries put with a ttl)
_RUNS: dict[tuple[str, str], tuple[tuple, asyncio.Task]] = {}


class RunSuperseded(Exception):
    """A newer run for the same customer and event cancelled this one."""


def cache_key(customer_id: str, event: str, params: dict, prompt_version: str, data_version: str) -> tuple:
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
    return (customer_id, event, digest, prompt_version, data_version)


def get(key: tuple) -> Any | None:
    if (exp := _EXPIRES.get(key)) is not None and time.monotonic() >= exp:
        _CACHE.pop(key, None)
        del _EXPIRES[key]
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
    return hit


def put(key: tuple, value: Any, ttl: float | None = None) -> None:
    """ttl: seconds until the entry expires (for results known to be temporary, such as a fallback); None keeps it until evicted."""
    _CACHE[key] = value
    if ttl is None:
        _EXPIRES.pop(key, None)
    else:
        _EXPIRES[key] = time.monotonic() + ttl
    _CACHE.move_to_end(key)
    while len(_CACHE) > MAX_ENTRIES:
        _CACHE.popitem(last=False)


def clear() -> None:
    _CACHE.clear()
    _EXPIRES.clear()
    for _, task in list(_RUNS.values()):
        try:
            if not task.done():
                task.cancel()
        except RuntimeError:                      # its event loop is already closed
            pass
    _RUNS.clear()
    from . import stream                      # progress logs of runs in flight (imported here: stream does not import the cache)
    stream.clear()


async def run_exclusive(customer_id: str, event: str, key: tuple, make: Callable[[], Awaitable[Any]], restart: bool = False) -> Any:
    """Run `make()` as the only task for (customer, event).

    A run already in flight for the SAME cache key is joined, not restarted (the sign-in warm-up and the page's own request are the
    same work). A run for a different key, or any call with restart=True (a refresh), cancels the old one; its callers get RunSuperseded.
    Callers that are themselves cancelled leave the shared task running, so its result still lands in the cache."""
    slot = (customer_id, event)
    current = _RUNS.get(slot)
    if current and not current[1].done() and (restart or current[0] != key):
        current[1].cancel()
        current = None
    if current is None or current[1].done():
        task = asyncio.ensure_future(make())
        _RUNS[slot] = current = (key, task)
        task.add_done_callback(lambda t, slot=slot: _RUNS.pop(slot, None) if _RUNS.get(slot, (None, None))[1] is t else None)
    task = current[1]
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        me = asyncio.current_task()
        if task.cancelled() and me is not None and me.cancelling() == 0:
            raise RunSuperseded() from None
        raise

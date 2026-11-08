"""Is the LLM server up? GET {base}/models, 2 s timeout, result cached for 30 s."""
from __future__ import annotations

import asyncio
import time

import httpx

from . import config

TIMEOUT_S, CACHE_S = 2.0, 30.0
_cache: tuple[float, dict] | None = None
_transport: httpx.AsyncBaseTransport | None = None       # tests inject a mock server here


def reset() -> None:
    global _cache
    _cache = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport
    reset()


async def _probe(http: httpx.AsyncClient, url: str) -> set[str] | None:
    try:
        r = await http.get(f"{url}/models", headers={"Authorization": f"Bearer {config.api_key()}"})
        r.raise_for_status()
        return {m.get("id") for m in r.json().get("data", [])}
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


async def llm_health() -> dict:
    """{"reachable": bool, "models_ready": bool}: whether every configured local server answers, and has every model it is
    configured to serve loaded."""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_S:
        return _cache[1]
    wanted: dict[str, set[str]] = {}
    for c in map(config.agent_config, config.AGENTS):
        if c.backend != "hosted":
            wanted.setdefault(c.base_url, set()).add(c.model)
    async with httpx.AsyncClient(transport=_transport, timeout=TIMEOUT_S) as http:
        have = dict(zip(wanted, await asyncio.gather(*(_probe(http, url) for url in wanted))))
    out = {"reachable": all(h is not None for h in have.values()),
           "models_ready": all(h is not None and wanted[u] <= h for u, h in have.items())}
    _cache = (now, out)
    return out
